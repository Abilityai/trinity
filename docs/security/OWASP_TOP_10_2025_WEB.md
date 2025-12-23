# OWASP Top 10:2025 - Web Application Security

> **Source**: [OWASP Top 10:2025](https://owasp.org/Top10/2025/)
> **Status**: Release Candidate (Final version expected 2026)
> **Last Updated**: December 2025

The OWASP Top 10 is the standard awareness document for web application security, representing broad consensus about the most critical security risks. The 2025 edition analyzed 589 CWEs across 248 categories.

---

## A01:2025 - Broken Access Control

**Rank Change**: Maintains #1 position

**Description**: Unauthorized access to resources or actions users shouldn't perform, including direct object references and server-side request forgery (SSRF). On average, 3.73% of applications tested had one or more of the 40 CWEs in this category.

**Key Examples**:
- Direct object reference vulnerabilities
- "Allow all" authorization predicates
- SSRF via internal services
- Missing server-side role enforcement

**Mitigation Strategies**:
- Enforce permission checks server-side only
- Validate that internal APIs cannot be accessed externally
- Whitelist approved hosts for SSRF-capable endpoints
- Regularly audit access rights for orphaned permissions

---

## A02:2025 - Security Misconfiguration

**Rank Change**: Climbed from #5 to #2

**Description**: Unsafe settings across frameworks, platforms, containers, and cloud environments that expose systems. Complexity is now a key adversary.

**Key Examples**:
- Default credentials left in production
- Debug endpoints enabled in live environments
- Publicly accessible cloud storage buckets
- Missing security headers (CSP, HSTS)

**Mitigation Strategies**:
- Remove default credentials from deployment pipelines
- Disable unnecessary features before production release
- Review Infrastructure-as-Code templates for public exposure
- Implement configuration scanning in CI/CD workflows

---

## A03:2025 - Software Supply Chain Failures

**Rank Change**: NEW category (expanded from A06:2021 "Vulnerable Components")

**Description**: Risks from compromised dependencies, malicious updates, and untrusted distribution channels. Acknowledges that risk extends beyond vulnerable code to the integrity of acquisition, build, and distribution processes.

**Key Examples**:
- Compromised third-party libraries
- Malicious code injected into build pipelines
- Unchecked transitive dependencies
- Integrity failures in distribution channels

**Mitigation Strategies**:
- Maintain a software bill of materials (SBOM)
- Verify digital signatures on all updates
- Monitor dependency alerts continuously
- Establish rapid patching processes for high-risk components

---

## A04:2025 - Cryptographic Failures

**Rank Change**: Dropped from #2 to #4

**Description**: Encryption weaknesses, improper key management, and insecure channels affecting data confidentiality.

**Key Examples**:
- Hard-coded encryption keys
- Weak algorithms (MD5, SHA1)
- Insecure random number generation
- Unencrypted sensitive data storage

**Mitigation Strategies**:
- Encrypt sensitive data at rest and in transit
- Use modern algorithms with appropriate key lengths
- Implement secure key rotation and lifecycle management
- Avoid home-grown cryptographic implementations

---

## A05:2025 - Injection

**Rank Change**: Dropped from #3 to #5

**Description**: Untrusted data sent to interpreters enabling unintended command execution across 38 CWE variants.

**Key Examples**:
- SQL injection attacks
- Cross-Site Scripting (XSS)
- OS command injection
- NoSQL and LDAP injection variants

**Mitigation Strategies**:
- Use parameterized queries instead of string concatenation
- Validate all user input rigorously
- Apply context-appropriate output encoding
- Deploy dynamic scanning tools for injection detection

---

## A06:2025 - Insecure Design

**Rank Change**: Dropped from #4 to #6

**Description**: Architectural and design flaws where threat modeling and security requirements were inadequately addressed. Noticeable improvements in the industry related to threat modeling have been observed since this category was introduced in 2021.

**Key Examples**:
- Missing threat modeling documentation
- Poor trust boundary definitions
- Business logic vulnerabilities (price manipulation)
- Insecure default configurations by design

**Mitigation Strategies**:
- Incorporate threat modeling early in project phases
- Document trust boundaries and data flows explicitly
- Review business logic for abnormal transaction flows
- Integrate security requirements into design documentation

---

## A07:2025 - Authentication Failures

**Rank Change**: Maintains #7 position (renamed from "Identification and Authentication Failures")

**Description**: Vulnerabilities in identity verification, session management, and credential handling enabling account takeover. Increased use of standardized frameworks appears to be having beneficial effects.

**Key Examples**:
- Weak password policies
- Missing multi-factor authentication (MFA)
- Session fixation vulnerabilities
- Insecure credential storage methods

**Mitigation Strategies**:
- Leverage robust authentication frameworks
- Enforce MFA for sensitive operations
- Implement secure session timeouts and invalidation
- Use salted hashing for password storage

---

## A08:2025 - Software or Data Integrity Failures

**Rank Change**: Maintains #8 position

**Description**: Failures to verify integrity of code, updates, data, and configurations within deployed systems. Focuses on maintaining trust boundaries.

**Key Examples**:
- Unsigned software updates
- Deserialization of untrusted data objects
- Unverified configuration file modifications
- Unvalidated plugin or extension loading

**Mitigation Strategies**:
- Verify digital signatures on all software updates
- Restrict deserialization to trusted data sources
- Audit build and deployment pipeline integrity
- Control third-party plugin sources strictly

---

## A09:2025 - Security Logging & Alerting Failures

**Rank Change**: Maintains #9 position (renamed to emphasize alerting)

**Description**: Insufficient logging, monitoring, and alerting infrastructure preventing incident detection and response. "Great logging with no alerting is of minimal value in identifying security incidents."

**Key Examples**:
- Missing security event logs
- No alerting mechanisms for suspicious activity
- Inadequate log retention periods
- Poor centralization of log data

**Mitigation Strategies**:
- Log security-relevant events comprehensively
- Establish real-time alerting for suspicious patterns
- Centralize logs across distributed systems
- Implement log integrity protection mechanisms

---

## A10:2025 - Mishandling of Exceptional Conditions

**Rank Change**: NEW category (contains 24 CWEs)

**Description**: Improper error handling, logical errors, failing open, and other scenarios stemming from abnormal conditions. Focuses on exception management and error information disclosure.

**Key Examples**:
- Detailed error messages revealing system information
- Unhandled exceptions exposing stack traces
- Sensitive data in error responses
- Exception swallowing without logging

**Mitigation Strategies**:
- Display generic error messages to end users
- Log detailed exceptions internally without exposing them
- Sanitize error responses before client delivery
- Implement comprehensive exception handling

---

## Key Changes from 2021

| 2021 | 2025 | Change |
|------|------|--------|
| A01 Broken Access Control | A01 Broken Access Control | Maintains #1, now includes SSRF |
| A05 Security Misconfiguration | A02 Security Misconfiguration | Jumped to #2 |
| A06 Vulnerable Components | A03 Software Supply Chain Failures | Expanded scope (NEW name) |
| A02 Cryptographic Failures | A04 Cryptographic Failures | Dropped 2 spots |
| A03 Injection | A05 Injection | Dropped 2 spots |
| A04 Insecure Design | A06 Insecure Design | Dropped 2 spots |
| A07 Identification and Auth Failures | A07 Authentication Failures | Renamed |
| A08 Software/Data Integrity Failures | A08 Software/Data Integrity Failures | Unchanged |
| A09 Logging Failures | A09 Security Logging & Alerting | Added alerting emphasis |
| A10 SSRF | A10 Mishandling of Exceptional Conditions | SSRF merged into A01 (NEW) |

---

## References

- [OWASP Top 10:2025 Official](https://owasp.org/Top10/2025/)
- [OWASP Foundation](https://owasp.org/www-project-top-ten/)
- [Indusface Analysis](https://www.indusface.com/learning/owasp-top-10-vulnerabilities/)
