---
name: add-testing
description: Add a Testing section to a feature flow document using the standard template.
allowed-tools: [Read, Edit]
user-invocable: true
argument-hint: "<feature-flow-name>"
automation: manual
---

# Add Testing Section to Feature Flow

Add a Testing section to a feature flow document using the standard template.

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| Feature Flow | `docs/memory/feature-flows/{name}.md` | ✅ | ✅ | Target document |

## Usage

```
/add-testing {feature-flow-name}
```

Example:
```
/add-testing scheduling
```

## Process

### Step 1: Read Feature Flow

Read the feature flow document (`docs/memory/feature-flows/{name}.md`)

### Step 2: Add Testing Section

Add a Testing section before "Related Flows" using the template:
- Prerequisites checklist
- Step-by-step test instructions
- Expected results
- Verification checklist
- Edge cases
- Cleanup steps
- Status tracking

### Step 3: Update Document

Use Edit tool to insert the section.

## Template

```markdown
## Testing

**Prerequisites**:
- [ ] [Service/prerequisite 1]
- [ ] [Service/prerequisite 2]

**Test Steps**:

### 1. [Action Name]
**Action**:
- Step 1
- Step 2

**Expected**: What should happen

**Verify**:
- [ ] Check 1
- [ ] Check 2

### 2. [Next Action]
...

**Edge Cases**:
- [ ] Test scenario 1
- [ ] Test scenario 2

**Cleanup**:
- [ ] Cleanup step 1

**Last Tested**: YYYY-MM-DD
**Tested By**: Not yet tested
**Status**: 🚧 Not Tested
**Issues**: None
```

## Notes

- Customize the template based on the specific feature
- Include UI, API, Docker, and Database verification where applicable
- Document both happy path and edge cases
- Keep instructions clear and actionable

## Completion Checklist

- [ ] Feature flow document read
- [ ] Testing section template customized
- [ ] Section inserted before "Related Flows"
- [ ] Document saved
