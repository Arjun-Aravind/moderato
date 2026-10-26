#!/usr/bin/env bash
# Orb runner. Use only a disposable Redis: the fault probe kills connections
# and flushes the script cache. No repository package imports are permitted.
set -euo pipefail
root=$(git rev-parse --show-toplevel)
out=${1:?Usage: bash tests/consumer/matrix.sh ABSOLUTE_OUTPUT_DIRECTORY}
mkdir -p "$out"
work=$(mktemp -d /tmp/moderato-qualification-XXXXXX)
cp "$root"/tests/consumer/{app,probe,provenance}.py "$work/"
wheel=$(realpath "$root"/dist/*.whl)
service="qualification-matrix-$$"
trap 'amp orb service stop "$service" >/dev/null 2>&1 || true' EXIT
failed=0
uv venv --python 3.12 "$work/resolver" > "$out/resolver-install.log" 2>&1
for incompatible in 0.46.0 0.46.1; do
    if uv pip install --dry-run --python "$work/resolver/bin/python" \
        "moderato[all] @ file://$wheel" "starlette==$incompatible" \
        > "$out/reject-$incompatible.log" 2>&1; then
        echo "FAIL: framework extra admitted Starlette $incompatible"
        failed=1
    elif ! grep -q 'No solution found' "$out/reject-$incompatible.log"; then
        echo "FAIL: could not verify dependency rejection for $incompatible"
        failed=1
    fi
done
for profile in ${PROFILES:-py310 py311 py312 py313 py314 minimum transitional}; do
    version=${profile#py}
    case "$profile" in
        py310|minimum) version=3.10;;
        py311) version=3.11;;
        py312|transitional) version=3.12;;
        py313) version=3.13;;
        py314) version=3.14;;
        *) echo "Unknown profile: $profile" >&2; exit 2;;
    esac
    uv venv --python "$version" "$work/$profile" > "$out/$profile-install.log" 2>&1
    python="$work/$profile/bin/python"
    pins=()
    if [[ $profile == minimum ]]; then
        pins=('redis==5.0.0' 'fastapi==0.100.0' 'starlette==0.27.0' 'prometheus-client==0.19.0' 'pydantic==2.0.2' 'anyio==3.6.2')
    elif [[ $profile == transitional ]]; then
        pins=('redis==5.2.1' 'fastapi==0.115.11' "starlette==${STARLETTE_VERSION:-0.46.2}" 'prometheus-client==0.21.1')
    fi
    if ! uv pip install --python "$python" "moderato[all] @ file://$wheel" httpx uvicorn "${pins[@]}" >> "$out/$profile-install.log" 2>&1; then
        failed=1
        continue
    fi
    uv pip check --python "$python" >> "$out/$profile-install.log" 2>&1
    prefix="qualification-$profile-$(cat /proc/sys/kernel/random/uuid)"
    amp orb service start "$service" --command "env REDIS_URL=redis://127.0.0.1:6379/12 QUALIFICATION_PROFILE=$profile QUALIFICATION_PREFIX=$prefix $python -I -m uvicorn app:app --app-dir $work --port 8092" >> "$out/$profile-server.log" 2>&1
    ready=0
    for _ in $(seq 1 50); do
        if curl -fsS http://127.0.0.1:8092/health > "$out/$profile-health.json"; then ready=1; break; fi
        sleep .2
    done
    if [[ $ready == 1 ]]; then
        env REDIS_URL=redis://127.0.0.1:6379/12 QUALIFICATION_PREFIX="$prefix-probe" \
            QUALIFICATION_PROFILE="$profile" EXPECTED_APP_PREFIX="$prefix" EXPECTED_SERVER_ENV="$work/$profile" \
            HTTP_URL=http://127.0.0.1:8092 FAULTS=0 SOAK_SECONDS=3 RESULT_PATH="$out/$profile.json" \
            "$python" -I "$work/probe.py" > "$out/$profile-probe.log" 2>&1 || failed=1
    else
        failed=1
    fi
    amp orb service logs "$service" -n 500 >> "$out/$profile-server.log" 2>&1
    amp orb service stop "$service"
done
echo "Consumer environments retained at $work" | tee "$out/environments.txt"
exit "$failed"
