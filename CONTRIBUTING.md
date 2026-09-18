# Contributing to Moderato

Thank you for your interest in contributing to Moderato! This document provides guidelines and instructions for contributing to the project.

---

## Table of Contents

1. [Code of Conduct](#code-of-conduct)
2. [Getting Started](#getting-started)
3. [Development Setup](#development-setup)
4. [Project Structure](#project-structure)
5. [Development Workflow](#development-workflow)
6. [Testing Guidelines](#testing-guidelines)
7. [Code Style](#code-style)
8. [Documentation](#documentation)
9. [Submitting Changes](#submitting-changes)
10. [Review Process](#review-process)
11. [Release Process](#release-process)

---

## Code of Conduct

### Our Pledge

We are committed to providing a welcoming and inclusive environment for all contributors. We expect:

- **Respectful Communication**: Be kind and constructive in all interactions
- **Collaborative Spirit**: Help others learn and grow
- **Professionalism**: Focus on what's best for the project
- **Inclusivity**: Welcome contributors of all backgrounds and skill levels

### Unacceptable Behavior

- Harassment, discrimination, or personal attacks
- Trolling, insulting comments, or unconstructive criticism
- Publishing others' private information
- Any conduct that could be considered inappropriate in a professional setting

### Enforcement

Violations should be reported to the project maintainers. All complaints will be reviewed and investigated promptly and fairly.

---

## Getting Started

### Prerequisites

Before contributing, ensure you have:

- **Python 3.9+** installed
- **Redis** server (for testing)
- **Git** for version control
- **Docker** (optional, for containerized Redis)

### First Contribution

If this is your first contribution:

1. **Look for "good first issue" labels** on the issue tracker
2. **Read the documentation** to understand the project
3. **Ask questions** if anything is unclear
4. **Start small** - fix a typo, improve docs, add a test

### Areas for Contribution

- - **Bug Fixes**: Fix issues reported in the issue tracker
- - **Features**: Implement new rate limiting algorithms or features
- - **Documentation**: Improve README, guides, or docstrings
- - **Tests**: Add test coverage or improve existing tests
- - **Performance**: Optimize algorithms or Redis operations
- - **Examples**: Create example integrations (FastAPI, Django, Flask)

---

## Development Setup

### 1. Fork and Clone

```bash
# Fork the repository on GitHub, then clone your fork
git clone https://github.com/YOUR_USERNAME/moderato.git
cd moderato

# Add upstream remote
git remote add upstream https://github.com/Arjun-Aravind/moderato.git
```

### 2. Create Virtual Environment

```bash
# Create virtual environment
python -m venv venv

# Activate (Linux/Mac)
source venv/bin/activate

# Activate (Windows)
venv\Scripts\activate
```

### 3. Install Dependencies

```bash
# Install development dependencies
pip install -e ".[dev,test,metrics]"

# This installs:
# - moderato package in editable mode
# - Development tools (black, ruff, mypy)
# - Testing tools (pytest, pytest-asyncio, pytest-cov)
# - Optional dependencies (prometheus-client)
```

### 4. Start Redis

**Option A: Local Redis**
```bash
# Install Redis (Ubuntu/Debian)
sudo apt-get install redis-server

# Start Redis
redis-server
```

**Option B: Docker**
```bash
# Start Redis container
docker run -d -p 6379:6379 redis:7-alpine

# Or use docker-compose
docker-compose up -d
```

### 5. Verify Setup

```bash
# Run tests to verify everything works
pytest

# Expected output:
# ========== 60+ passed in 2.5s ==========
```

---

## Project Structure

```
moderato/
├── moderato/                  # Main package
│   ├── __init__.py            # Public API exports
│   ├── limiter.py             # RateLimiter class (main interface)
│   ├── models.py              # Data models (RateLimitConfig, etc.)
│   ├── exceptions.py          # Custom exceptions
│   ├── utils.py               # Utility functions
│   ├── decorators.py          # Decorator implementation
│   ├── middleware.py          # FastAPI/Starlette middleware
│   ├── metrics.py             # Prometheus metrics
│   │
│   ├── algorithms/            # Rate limiting algorithms
│   │   ├── __init__.py
│   │   ├── base.py            # Base algorithm interface
│   │   ├── token_bucket.py    # Token Bucket implementation
│   │   └── sliding_window.py  # Sliding Window implementation
│   │
│   ├── backends/              # Storage backends
│   │   ├── __init__.py
│   │   └── redis.py           # Redis backend with Lua scripts
│   │
│   └── scripts/               # Lua scripts
│       ├── fixed_window.lua
│       ├── token_bucket.lua
│       └── sliding_window.lua
│
├── tests/                     # Test suite
│   ├── conftest.py            # Pytest fixtures
│   ├── test_limiter.py        # RateLimiter tests
│   ├── test_algorithms.py     # Algorithm tests
│   ├── test_token_bucket.py   # Token Bucket tests
│   ├── test_sliding_window.py # Sliding Window tests
│   ├── test_middleware.py     # Middleware tests
│   ├── test_backends.py       # Backend tests
│   └── test_utils.py          # Utility tests
│
├── examples/                  # Example integrations
│   ├── fastapi_basic.py
│   ├── fastapi_advanced.py
│   └── docker-compose.yml
│
├── docs/                      # Documentation
│   ├── ALGORITHMS.md          # Algorithm deep dive
│   ├── ARCHITECTURE.md        # Architecture details
│   └── CONTRIBUTING.md        # This file
│
├── pyproject.toml             # Project metadata and dependencies
├── README.md                  # Project overview
├── LICENSE                    # MIT License
└── .github/                   # GitHub-specific files
    └── workflows/
        └── ci.yml             # CI/CD pipeline
```

---

## Development Workflow

### 1. Create a Feature Branch

```bash
# Update main branch
git checkout main
git pull upstream main

# Create feature branch
git checkout -b feature/your-feature-name

# Or for bug fixes
git checkout -b fix/issue-123
```

### 2. Make Changes

- Write clean, readable code
- Follow existing code style
- Add tests for new functionality
- Update documentation as needed

### 3. Run Tests Locally

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=moderato --cov-report=html

# Run specific test file
pytest tests/test_limiter.py

# Run specific test
pytest tests/test_limiter.py::test_basic_rate_limiting
```

### 4. Run Linters

```bash
# Format code with black
black moderato/ tests/

# Lint with ruff
ruff check moderato/ tests/

# Type check with mypy
mypy moderato/ --strict
```

### 5. Commit Changes

```bash
# Stage changes
git add .

# Commit with descriptive message
git commit -m "feat: add new rate limiting algorithm"

# See commit message guidelines below
```

### 6. Push and Create PR

```bash
# Push to your fork
git push origin feature/your-feature-name

# Go to GitHub and create a Pull Request
```

---

## Testing Guidelines

### Test Structure

Tests are organized by component:

- `test_limiter.py`: Main RateLimiter functionality
- `test_algorithms.py`: Algorithm-specific tests
- `test_backends.py`: Redis backend tests
- `test_middleware.py`: Middleware tests
- `test_utils.py`: Utility function tests

### Writing Tests

#### Unit Tests

Test individual functions/methods in isolation:

```python
def test_parse_rate():
    """Test rate string parsing."""
    requests, seconds = parse_rate("100/minute")
    assert requests == 100
    assert seconds == 60

def test_parse_rate_invalid():
    """Test invalid rate format."""
    with pytest.raises(ValueError):
        parse_rate("invalid")
```

#### Integration Tests

Test full workflows with Redis:

```python
@pytest.mark.asyncio
async def test_rate_limit_enforcement(limiter):
    """Test that rate limiting works end-to-end."""
    # Make requests up to limit
    for _ in range(100):
        await limiter.check("user:123", "100/minute")

    # Next request should fail
    with pytest.raises(RateLimitExceeded):
        await limiter.check("user:123", "100/minute")
```

#### Concurrency Tests

Test thread-safety and race conditions:

```python
@pytest.mark.asyncio
async def test_concurrent_requests(limiter):
    """Test multiple concurrent requests."""
    async def make_request():
        try:
            await limiter.check("user:concurrent", "100/minute")
            return True
        except RateLimitExceeded:
            return False

    # 200 concurrent requests
    tasks = [make_request() for _ in range(200)]
    results = await asyncio.gather(*tasks)

    # Exactly 100 should succeed
    assert sum(results) == 100
```

#### Parametrized Tests

Test multiple scenarios:

```python
@pytest.mark.parametrize("algorithm", ["fixed_window", "token_bucket", "sliding_window"])
@pytest.mark.asyncio
async def test_all_algorithms(limiter, algorithm):
    """Test all algorithms behave correctly."""
    await limiter.check("user:test", "100/minute", algorithm=algorithm)
```

### Test Fixtures

Use pytest fixtures for common setup:

```python
# conftest.py
@pytest.fixture
async def limiter():
    """Create rate limiter for testing."""
    limiter = RateLimiter(redis_url="redis://localhost:6379/15")
    await limiter.connect()
    yield limiter
    await limiter.close()

@pytest.fixture
async def clean_redis(limiter):
    """Clean Redis database before test."""
    await limiter.backend._redis.flushdb()
    yield
```

### Coverage Requirements

- **Minimum coverage**: 80%
- **Target coverage**: 90%+
- **Critical paths**: 100% (rate limiting logic, Lua scripts)

Check coverage:
```bash
pytest --cov=moderato --cov-report=term-missing
```

---

## Code Style

### Python Style Guide

We follow **PEP 8** with some modifications:

- **Line length**: 100 characters (not 79)
- **String quotes**: Double quotes `"` preferred
- **Imports**: Grouped (stdlib, third-party, local)
- **Type hints**: Required for all public functions

### Formatting Tools

**Black** (automatic formatting):
```bash
black moderato/ tests/
```

**Ruff** (linting):
```bash
ruff check moderato/ tests/ --fix
```

**Mypy** (type checking):
```bash
mypy moderato/ --strict
```

### Code Conventions

#### Naming

```python
# Classes: PascalCase
class RateLimiter:
    pass

# Functions/methods: snake_case
def check_rate_limit():
    pass

# Constants: UPPER_SNAKE_CASE
MAX_RETRIES = 3

# Private: leading underscore
def _internal_method():
    pass
```

#### Type Hints

Always use type hints:

```python
# Good
async def check(
    self,
    key: str,
    rate: str,
    algorithm: Optional[str] = None,
    cost: int = 1,
) -> bool:
    pass

# Bad (no type hints)
async def check(self, key, rate, algorithm=None, cost=1):
    pass
```

#### Docstrings

Use Google-style docstrings:

```python
def parse_rate(rate: str) -> tuple[int, int]:
    """
    Parse rate limit string into requests and window.

    Args:
        rate: Rate limit string (e.g., "100/minute", "1000/hour")

    Returns:
        Tuple of (requests, window_seconds)

    Raises:
        ValueError: If rate format is invalid

    Examples:
        >>> parse_rate("100/minute")
        (100, 60)

        >>> parse_rate("1000/hour")
        (1000, 3600)
    """
    pass
```

#### Error Handling

```python
# Good: Specific exceptions
try:
    result = await redis.get(key)
except redis.ConnectionError as e:
    raise BackendError(f"Redis connection failed: {e}") from e

# Bad: Bare except
try:
    result = await redis.get(key)
except:  # Don't do this!
    pass
```

---

## Documentation

### Types of Documentation

1. **Code Comments**: Explain complex logic
2. **Docstrings**: Document all public functions/classes
3. **README**: Project overview and quick start
4. **ALGORITHMS.md**: Algorithm deep dive
5. **ARCHITECTURE.md**: System internals
6. **Examples**: Working code samples

### Writing Good Docstrings

```python
async def check(
    self,
    key: str,
    rate: str,
    algorithm: Optional[str] = None,
    tenant_type: Optional[str] = None,
    cost: int = 1,
) -> bool:
    """
    Check if a request is allowed under the rate limit.

    This is the core method for rate limiting. It checks whether
    a request identified by `key` is allowed under the specified
    rate limit.

    Args:
        key: Unique identifier for the rate limit (e.g., user ID, IP address)
        rate: Rate limit string (e.g., "100/minute", "1000/hour")
        algorithm: Algorithm to use (defaults to config.default_algorithm)
        tenant_type: Tenant type for multi-tenant setups (e.g., "free", "premium")
        cost: Cost of this request (default 1, can be higher for expensive operations)

    Returns:
        True if request is allowed

    Raises:
        RateLimitExceeded: If rate limit is exceeded
        RateLimitConfigError: If configuration is invalid
        BackendError: If backend operation fails

    Examples:
        Simple check:
        >>> await limiter.check(key="user:123", rate="100/minute")
        True

        Multi-tenant check:
        >>> await limiter.check(
        ...     key="api:key:abc123",
        ...     rate="1000/hour",
        ...     tenant_type="premium"
        ... )
        True

        Higher cost operation:
        >>> await limiter.check(
        ...     key="user:123",
        ...     rate="100/minute",
        ...     cost=10  # This request counts as 10 regular requests
        ... )
        True
    """
    pass
```

### Updating Documentation

When making changes:

1. **Update docstrings** if function signature changes
2. **Update README** if adding new features
3. **Update ALGORITHMS.md** if modifying algorithm behavior
4. **Update ARCHITECTURE.md** if changing system design
5. **Add examples** for new features

---

## Submitting Changes

### Commit Message Guidelines

Follow **Conventional Commits**:

```
<type>(<scope>): <subject>

<body>

<footer>
```

**Types**:
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `test`: Adding or updating tests
- `refactor`: Code refactoring (no behavior change)
- `perf`: Performance improvements
- `chore`: Maintenance tasks

**Examples**:

```
feat(algorithms): add sliding window algorithm

Implement sliding window rate limiting with weighted average.
This provides more accurate rate limiting than fixed window
while being more efficient than token bucket.

Closes #42
```

```
fix(backend): handle script eviction gracefully

Redis can evict Lua scripts under memory pressure. Add fallback
to reload scripts if EVALSHA fails with NoScriptError.

Fixes #123
```

```
docs(readme): add token bucket examples

Add usage examples for token bucket algorithm, including
cost-based rate limiting and burst handling.
```

### Pull Request Guidelines

1. **Create an issue first** for significant changes
2. **Reference the issue** in your PR description
3. **Describe your changes** clearly
4. **Include tests** for new functionality
5. **Update documentation** as needed
6. **Keep PRs focused** - one feature/fix per PR

### PR Template

```markdown
## Description
Brief description of changes

## Motivation
Why is this change needed?

## Type of Change
- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] Documentation update

## Testing
- [ ] Unit tests added/updated
- [ ] Integration tests added/updated
- [ ] All tests pass locally

## Checklist
- [ ] Code follows project style guidelines
- [ ] Self-review completed
- [ ] Comments added for complex logic
- [ ] Documentation updated
- [ ] No new warnings generated
```

---

## Review Process

### What to Expect

1. **Initial Review**: Within 1-3 days
2. **Feedback**: Constructive suggestions for improvement
3. **Iteration**: You may need to make changes
4. **Approval**: Once changes meet requirements
5. **Merge**: Maintainer will merge your PR

### Review Criteria

- **Correctness**: Does it work as intended?
- **Tests**: Are there adequate tests?
- **Code Quality**: Is it readable and maintainable?
- **Documentation**: Is it properly documented?
- **Performance**: Does it introduce performance issues?
- **Breaking Changes**: Are they necessary and documented?

### Responding to Feedback

- **Be Open**: Feedback helps improve the project
- **Ask Questions**: If something is unclear
- **Be Respectful**: Even if you disagree
- **Iterate**: Make requested changes or discuss alternatives

---

## Release Process

### Versioning

We follow **Semantic Versioning** (SemVer):

```
MAJOR.MINOR.PATCH

Example: 1.2.3
         │ │ │
         │ │ └─ Patch: Bug fixes
         │ └─── Minor: New features (backward compatible)
         └───── Major: Breaking changes
```

### Release Checklist

1. **Update Version**: In `pyproject.toml`
2. **Update CHANGELOG**: Document all changes
3. **Run Full Test Suite**: Ensure all tests pass
4. **Update Documentation**: Ensure docs are current
5. **Create Git Tag**: `git tag v1.2.3`
6. **Build Package**: `python -m build`
7. **Publish to PyPI**: `python -m twine upload dist/*`

### Changelog Format

```markdown
## [1.2.3] - 2024-01-15

### Added
- New sliding window algorithm
- Support for cost-based rate limiting

### Fixed
- Fixed race condition in token bucket refill
- Corrected TTL edge case handling

### Changed
- Improved error messages
- Updated dependencies

### Deprecated
- Old config format (will be removed in 2.0.0)

### Removed
- Support for Python 3.8

### Security
- Fixed potential key injection vulnerability
```

---

## Development Tips

### Running Redis in Docker

```bash
# Start Redis
docker run -d -p 6379:6379 --name redis-dev redis:7-alpine

# Check logs
docker logs redis-dev

# Connect with redis-cli
docker exec -it redis-dev redis-cli

# Stop Redis
docker stop redis-dev

# Remove container
docker rm redis-dev
```

### Debugging Tests

```python
# Add breakpoints
import pdb; pdb.set_trace()

# Or use pytest's built-in debugger
pytest --pdb

# Run with verbose output
pytest -vv

# Run specific test with output
pytest tests/test_limiter.py::test_basic_rate_limiting -s
```

### Profiling Performance

```python
import cProfile
import pstats

async def profile_check():
    limiter = RateLimiter()
    await limiter.connect()

    # Profile 1000 checks
    profiler = cProfile.Profile()
    profiler.enable()

    for _ in range(1000):
        try:
            await limiter.check("user:123", "100/minute")
        except RateLimitExceeded:
            pass

    profiler.disable()
    stats = pstats.Stats(profiler)
    stats.sort_stats('cumtime')
    stats.print_stats(20)

    await limiter.close()
```

### Useful Commands

```bash
# Install in editable mode
pip install -e .

# Install with all extras
pip install -e ".[dev,test,metrics]"

# Run tests with coverage
pytest --cov=moderato --cov-report=html

# Open coverage report
open htmlcov/index.html  # Mac
xdg-open htmlcov/index.html  # Linux

# Format all code
black moderato/ tests/ examples/

# Lint and auto-fix
ruff check moderato/ tests/ --fix

# Type check
mypy moderato/ --strict

# Build package
python -m build

# Check package
twine check dist/*
```

---

## Getting Help

### Resources

- **Documentation**: README.md, ALGORITHMS.md, ARCHITECTURE.md
- **Examples**: `examples/` directory
- **Tests**: `tests/` directory (great for learning usage)
- **Issue Tracker**: Report bugs or request features

### Asking Questions

When asking questions:

1. **Search first**: Check if it's already answered
2. **Be specific**: Provide details and context
3. **Include code**: Share relevant code snippets
4. **Show effort**: Explain what you've tried

### Community

- **GitHub Issues**: For bug reports and feature requests
- **GitHub Discussions**: For questions and general discussion
- **Pull Requests**: For code contributions

---

## License

By contributing to Moderato, you agree that your contributions will be licensed under the MIT License.

---

## Thank You! 

Thank you for contributing to Moderato! Your contributions help make rate limiting better for everyone.

**Happy Coding!** 
