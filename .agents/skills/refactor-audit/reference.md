# Refactoring Best Practices Reference

> Research compiled 2026-02-22 for AI-maintained codebases

## Sources

- [Agentic AI Coding: Best Practice Patterns](https://codescene.com/blog/agentic-ai-coding-best-practice-patterns-for-speed-with-quality) - CodeScene
- [AI Code Refactoring: Strategic Approaches](https://getdx.com/blog/enterprise-ai-refactoring-best-practices/) - DX
- [How to Use AI in Coding - 12 Best Practices](https://zencoder.ai/blog/how-to-use-ai-in-coding) - Zencoder
- [FastAPI Best Practices](https://github.com/zhanymkanov/fastapi-best-practices) - GitHub
- [Radon Documentation](https://radon.readthedocs.io/en/latest/intro.html) - Code Metrics
- [Cyclomatic Complexity](https://en.wikipedia.org/wiki/Cyclomatic_complexity) - Wikipedia/NIST
- [Code Smells](https://www.sonarsource.com/resources/library/code-smells/) - SonarSource
- [Context Windows Engineering](https://www.kinde.com/learn/ai-for-software-engineering/best-practice/ai-context-windows-engineering-around-token-limits-in-large-codebases/) - Kinde

---

## 1. AI-Friendly Code Principles

### Code Health Score
AI performs best on healthy, modular code. Target metrics:
- **Code Health Score**: ≥9.5/10 (CodeScene)
- **Maintainability Index**: A or B grade

### Context Window Considerations
- ~1,000 tokens ≈ 750 words ≈ **40 lines of code**
- Files >1,000 lines strain AI context
- Functions >100 lines hard to refactor safely
- Chunk by function/class, not arbitrary line counts

### What Makes Code AI-Unfriendly
1. **Large monolithic files** - Can't fit in context
2. **Deep nesting** - Confuses AI like humans
3. **Unclear boundaries** - AI can't determine scope
4. **Missing tests** - No safety net for changes
5. **Complex conditionals** - Hard to reason about

---

## 2. Complexity Thresholds

### Cyclomatic Complexity (McCabe)
Measures decision points + 1. Industry-standard metric.

| CC Score | Grade | Interpretation | Action |
|----------|-------|----------------|--------|
| 1-5 | A | Simple, low risk | ✅ Good |
| 6-10 | B | Moderate complexity | ✅ Acceptable |
| 11-15 | C | Complex, harder to test | ⚠️ Consider refactoring |
| 16-20 | D | High complexity | 🔴 Refactor recommended |
| 21-30 | E | Very high complexity | 🔴 Must refactor |
| >30 | F | Untestable | 🚨 Critical - split immediately |

**NIST Recommendation**: Keep CC ≤10 for most modules. Allow up to 15 only with experienced staff and comprehensive tests.

### Cognitive Complexity (SonarQube)
Measures how hard code is to understand. Penalizes nesting.

| Score | Interpretation | Action |
|-------|----------------|--------|
| <15 | Acceptable | ✅ Good |
| 15-20 | Code smell | ⚠️ Refactor |
| >20 | Quality gate fail | 🔴 Must refactor |

### Function/Method Length

| Lines | Interpretation | Action |
|-------|----------------|--------|
| <30 | Ideal | ✅ Excellent |
| 30-50 | Good | ✅ Acceptable |
| 50-100 | Long | ⚠️ Consider splitting |
| 100-200 | Very long | 🔴 Refactor recommended |
| >200 | Excessive | 🚨 Critical - must split |

**Rule of thumb**: If it doesn't fit on one screen (~50 lines), split it.

### File Length

| Lines | Interpretation | Action |
|-------|----------------|--------|
| <300 | Ideal | ✅ Excellent |
| 300-500 | Good | ✅ Acceptable |
| 500-800 | Large | ⚠️ Consider splitting |
| 800-1000 | Very large | 🔴 Refactor recommended |
| >1000 | Excessive | 🚨 Critical - AI struggles |

---

## 3. Code Smells to Detect

### Structural Smells

| Smell | Detection | Threshold |
|-------|-----------|-----------|
| **Long Method** | Line count | >50-100 lines |
| **Large Class/File** | Line count | >500-1000 lines |
| **Deep Nesting** | Indentation levels | >4 levels |
| **Long Parameter List** | Param count | >5 parameters |
| **God Class** | Methods + responsibilities | >20 public methods |
| **Feature Envy** | External field access | Heuristic analysis |

### Duplication Smells

| Smell | Detection | Threshold |
|-------|-----------|-----------|
| **Duplicate Code** | Token similarity | >6 duplicated lines |
| **Copy-Paste Inheritance** | Repeated patterns | Similar methods across files |
| **Magic Numbers** | Hardcoded values | Any unexplained literal |

### Maintainability Smells

| Smell | Detection | Impact |
|-------|-----------|--------|
| **Dead Code** | Unused functions/imports | Confuses AI, wastes context |
| **Commented Code** | Large comment blocks | Should be removed |
| **Inconsistent Naming** | Style violations | Hurts readability |
| **Missing Type Hints** | No annotations | Harder for AI to reason |

---

## 4. FastAPI/Python Specific Patterns

### Good Patterns (AI-Friendly)

```python
# ✅ Single responsibility - one thing per function
async def get_user(user_id: UUID) -> User:
    return await db.users.get(user_id)

# ✅ Dependency injection - reusable, testable
async def valid_user(user_id: UUID = Path(...)) -> User:
    user = await get_user(user_id)
    if not user:
        raise HTTPException(404)
    return user

# ✅ Type hints everywhere
def calculate_cost(tokens: int, model: str) -> Decimal:
    ...
```

### Anti-Patterns (Refactoring Candidates)

```python
# ❌ God function - does too much
async def process_request(request):
    # Validate input (should be Pydantic)
    # Fetch from database (should be service)
    # Apply business logic (should be service)
    # Format response (should be schema)
    # Log and audit (should be middleware)
    ...

# ❌ Deep nesting
if a:
    if b:
        for x in items:
            if c:
                if d:  # 5 levels deep!
                    ...

# ❌ Long parameter list
def create_agent(name, type, template, owner, credentials,
                 permissions, schedules, folders, tags, ...):
    ...  # Should be a config object
```

### Project Structure (Feature-Based)

```
src/
├── agents/           # Feature module
│   ├── router.py     # HTTP endpoints only
│   ├── schemas.py    # Pydantic models
│   ├── service.py    # Business logic
│   ├── dependencies.py
│   └── exceptions.py
├── schedules/        # Another feature
├── credentials/      # Another feature
└── shared/           # Cross-cutting concerns
```

---

## 5. Vue.js/Frontend Patterns

### Good Patterns

| Pattern | Description |
|---------|-------------|
| **Small components** | <300 lines per .vue file |
| **Composables** | Extract reusable logic to `use*.js` |
| **Props validation** | Define prop types and defaults |
| **Emit typing** | Document emitted events |

### Refactoring Candidates

| Smell | Threshold | Action |
|-------|-----------|--------|
| Large component | >400 lines | Extract child components |
| Long setup() | >100 lines | Extract composables |
| Prop drilling | >3 levels | Use provide/inject or store |
| Inline styles | Scattered | Move to Tailwind classes |

---

## 6. AI Refactoring Guardrails

### Before AI Refactoring

1. **Lock behavior with tests**
   - Characterization tests for existing behavior
   - Snapshot tests for complex outputs
   - Regression tests for critical paths

2. **Set clear constraints**
   - Do NOT change public interfaces
   - Do NOT alter business logic without tests
   - Do NOT touch auth/crypto/payments without review

3. **Assess AI-readiness**
   - Is code health ≥9.5?
   - Are functions <100 lines?
   - Are files <1000 lines?

### During AI Refactoring

1. **One PR = One intent**
   - Narrow diffs (function, file, or module)
   - Ship sequences of small improvements
   - Never attempt "perfect" rewrites

2. **Real-time validation**
   - Pre-commit hooks check quality
   - Coverage gates on PRs
   - Automated code health checks

### After AI Refactoring

1. **Verify behavior preserved**
   - All tests pass
   - No new warnings/errors
   - Manual smoke test critical paths

---

## 7. Metrics Collection Tools

### Python

| Tool | Metrics | Command |
|------|---------|---------|
| **Radon** | CC, MI, raw | `radon cc -a -s src/` |
| **Pylint** | Style, errors | `pylint src/` |
| **Flake8** | Style, complexity | `flake8 --max-complexity 10` |
| **Vulture** | Dead code | `vulture src/` |
| **Bandit** | Security | `bandit -r src/` |

### JavaScript/TypeScript

| Tool | Metrics | Command |
|------|---------|---------|
| **ESLint** | Style, complexity | `eslint src/` |
| **jscpd** | Duplication | `jscpd src/` |
| **Plato** | Complexity report | `plato -r -d report src/` |

### Cross-Language

| Tool | Metrics |
|------|---------|
| **SonarQube** | All metrics, smells, security |
| **CodeClimate** | Maintainability, duplication |
| **CodeScene** | Code health, hotspots |

---

## 8. Trinity-Specific Thresholds

Based on codebase analysis and AI maintenance needs:

### Backend (Python/FastAPI)

| Metric | Warning | Critical |
|--------|---------|----------|
| File lines | >500 | >800 |
| Function lines | >50 | >100 |
| Cyclomatic complexity | >10 | >15 |
| Parameters | >5 | >7 |
| Nesting depth | >3 | >4 |

### Frontend (Vue.js)

| Metric | Warning | Critical |
|--------|---------|----------|
| Component lines | >300 | >500 |
| Setup/script lines | >100 | >200 |
| Props count | >8 | >12 |
| Template depth | >5 | >8 |

### Duplication

| Metric | Warning | Critical |
|--------|---------|----------|
| Duplicate lines | >6 | >10 |
| Duplicate blocks | >2 files | >3 files |
| Copy-paste ratio | >3% | >5% |

---

## 9. Prioritization Matrix

When multiple issues exist, prioritize by:

| Priority | Category | Rationale |
|----------|----------|-----------|
| 🚨 P0 | Security issues | Must fix immediately |
| 🔴 P1 | Critical complexity (>30 CC) | Untestable, high bug risk |
| 🔴 P1 | Files >1000 lines | AI can't process |
| ⚠️ P2 | High complexity (15-30 CC) | Maintenance burden |
| ⚠️ P2 | Significant duplication (>5%) | Bug propagation risk |
| 📝 P3 | Moderate issues (10-15 CC) | Technical debt |
| 📝 P4 | Style/consistency | Nice to have |

---

## 10. Refactoring Patterns

### Extract Method
**When**: Function >50 lines or does multiple things
**How**: Identify cohesive blocks, extract with clear names

### Extract Class/Module
**When**: File >500 lines or class has multiple responsibilities
**How**: Group related methods, create new module

### Replace Conditional with Polymorphism
**When**: Large switch/if-else chains
**How**: Use strategy pattern or dispatch tables

### Introduce Parameter Object
**When**: >5 parameters
**How**: Create config/options dataclass

### Decompose Conditional
**When**: Complex nested conditions
**How**: Extract conditions to well-named functions

### Replace Magic Numbers
**When**: Hardcoded literals
**How**: Extract to named constants
