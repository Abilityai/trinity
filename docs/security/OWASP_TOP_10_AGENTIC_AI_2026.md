# OWASP Top 10 for Agentic Applications 2026

> **Source**: [OWASP GenAI Security Project](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/)
> **Released**: December 10, 2025 at Black Hat Europe
> **Contributors**: 100+ security researchers, industry practitioners, and technology providers
> **Last Updated**: December 2025

The OWASP Top 10 for Agentic Applications identifies critical security risks specific to autonomous AI agents that plan, execute, and delegate tasks independently. This framework addresses the unique challenges of "System 2" AI that operates with deliberative reasoning.

---

## ASI01 - Agent Goal Hijack

**Description**: Attackers alter agent objectives through malicious text content. Agents struggle to distinguish instructions from data, leading to unintended actions.

**Attack Vectors**:
- Indirect prompt injection through retrieved documents
- Poisoned data in RAG systems
- Manipulated calendar invites or emails affecting scheduling agents
- Malicious content in databases agents query

**Examples**:
- Financial agent manipulated into sending money to attacker
- Research agent exfiltrating sensitive data
- Scheduling agent canceling critical meetings

**Mitigation Strategies**:
- Treat all natural language input as untrusted
- Apply prompt injection filtering and detection
- Limit tool privileges to minimum required
- Require human approval for goal changes
- Implement goal verification checkpoints

---

## ASI02 - Tool Misuse and Exploitation

**Description**: Agents use legitimate tools unsafely due to ambiguous prompts, misalignment, or poisoned inputs, resulting in destructive parameter calls or unexpected tool chaining.

**Attack Vectors**:
- Over-privileged tools affecting production systems
- Compromised tool descriptors in MCP servers
- Unvalidated shell command execution
- Tool chaining creating unintended side effects

**Examples**:
- Database tool deleting production tables
- File system tool accessing sensitive directories
- API tool making unauthorized external requests

**Mitigation Strategies**:
- Strict tool permission scoping (least privilege)
- Sandboxed execution environments
- Argument validation and sanitization
- Policy controls on all tool invocations
- Tool usage logging and monitoring

---

## ASI03 - Identity and Privilege Abuse

**Description**: Agents inherit high-privilege credentials and tokens, which can be unintentionally reused, escalated, or passed across agents without proper scoping.

**Attack Vectors**:
- SSH keys cached in agent memory
- Unscoped cross-agent delegation
- Confused deputy scenarios
- Token forwarding without authorization

**Examples**:
- Agent using admin credentials for routine tasks
- Delegated agent inheriting full parent permissions
- API tokens reused across security boundaries

**Mitigation Strategies**:
- Short-lived credentials with automatic expiration
- Task-scoped permissions per operation
- Policy-enforced authorization checks
- Isolated agent identities
- Credential rotation and revocation

---

## ASI04 - Agentic Supply Chain Vulnerabilities

**Description**: Tools, plugins, prompt templates, model files, and MCP servers fetched at runtime create compromise risks affecting agent behavior and data exposure.

**Attack Vectors**:
- Malicious MCP servers impersonating trusted tools
- Poisoned prompt templates
- Vulnerable third-party agents in workflows
- Compromised model weights or adapters

**Examples**:
- Fake MCP server intercepting agent communications
- Malicious tool descriptor altering agent behavior
- Backdoored plugin exfiltrating data

**Mitigation Strategies**:
- Signed manifests for all components
- Curated registries with verification
- Dependency pinning with hash verification
- Sandboxing for untrusted components
- Kill switches for compromised components

---

## ASI05 - Unexpected Code Execution (RCE)

**Description**: Agents generate or run code and commands unsafely, including shell scripts, migrations, template evaluation, or unsafe deserialization.

**Attack Vectors**:
- Code assistants running generated patches directly
- Prompt injection triggering shell commands
- Unsafe deserialization in memory systems
- Dynamic code evaluation without sandboxing

**Examples**:
- Agent executing `rm -rf /` from injected prompt
- SQL migration deleting all data
- Python eval() on user-controlled input

**Mitigation Strategies**:
- Treat all generated code as untrusted
- Remove direct evaluation capabilities
- Use hardened sandboxes (gVisor, Firecracker)
- Require preview/review steps before execution
- Implement code analysis before execution

---

## ASI06 - Memory and Context Poisoning

**Description**: Attackers poison agent memory systems, embeddings, RAG databases, and summaries to influence future decisions and behavior.

**Attack Vectors**:
- RAG database poisoning with malicious content
- Cross-tenant context leakage
- Long-term drift from repeated adversarial exposure
- Summary manipulation affecting future reasoning

**Examples**:
- Malicious document in RAG causing persistent misbehavior
- Poisoned conversation history affecting all future sessions
- Embedding manipulation altering retrieval results

**Mitigation Strategies**:
- Segment memory by trust level and source
- Filter content before ingestion
- Track provenance of all stored information
- Expire suspicious entries automatically
- Regular memory audits and cleanup

---

## ASI07 - Insecure Inter-Agent Communication

**Description**: Multi-agent message exchanges lack authentication, encryption, or semantic validation, enabling interception or instruction injection.

**Attack Vectors**:
- Spoofed agent identities
- Replayed delegation messages
- Tampering on unprotected channels
- Man-in-the-middle attacks between agents

**Examples**:
- Attacker impersonating orchestrator agent
- Replay of authorization messages
- Injection of malicious tasks via compromised channel

**Mitigation Strategies**:
- Mutual TLS for all agent communication
- Signed payloads with verification
- Anti-replay protections (nonces, timestamps)
- Authenticated discovery mechanisms
- Message integrity verification

---

## ASI08 - Cascading Failures

**Description**: Errors in one agent propagate across planning, execution, memory, and downstream systems due to interconnected agent architecture.

**Attack Vectors**:
- Hallucinating planners issuing destructive tasks
- Poisoned state propagating through workflow
- Error amplification through agent chains
- Failure recovery triggering additional damage

**Examples**:
- Planning agent error causing all workers to fail
- Single poisoned agent corrupting shared memory
- Retry logic amplifying destructive actions

**Mitigation Strategies**:
- Isolation boundaries between agents
- Rate limits on agent actions
- Circuit breakers for failure detection
- Pre-deployment multi-step plan testing
- Graceful degradation strategies

---

## ASI09 - Human-Agent Trust Exploitation

**Description**: Users over-trust agent recommendations or explanations, allowing attackers or misaligned agents to influence decisions or extract sensitive information.

**Attack Vectors**:
- Social engineering through agent personas
- Subtle backdoors in code suggestions
- Fraudulent transaction approvals
- Credential extraction through rapport

**Examples**:
- Code assistant introducing vulnerable dependencies
- Financial copilot approving fraudulent transfers
- Support agent phishing for user credentials

**Mitigation Strategies**:
- Forced confirmations for sensitive actions
- Immutable audit logs of all recommendations
- Clear risk indicators in UI
- Avoid persuasive language in critical workflows
- User education on agent limitations

---

## ASI10 - Rogue Agents

**Description**: Compromised or misaligned agents act harmfully while appearing legitimate, persisting across sessions or impersonating other agents.

**Characteristics**:
- Self-repeating actions without authorization
- Persistence across sessions
- Impersonation of other agents
- Covert data exfiltration

**Examples**:
- Agent continuing data theft after single injection
- Silent approval of unsafe actions
- Backup deletion justified as "cost optimization"
- Agent spawning unauthorized child agents

**Mitigation Strategies**:
- Strict governance and oversight
- Comprehensive sandboxing
- Behavioral monitoring and anomaly detection
- Kill switches for immediate termination
- Regular agent integrity verification

---

## Key Principle: Least Agency

**The framework emphasizes granting agents only the minimum autonomy required for safe, bounded tasks.**

| Principle | Implementation |
|-----------|----------------|
| Minimal Functionality | Only enable tools needed for specific task |
| Minimal Permissions | Scope credentials to exact requirements |
| Minimal Autonomy | Require approval for consequential actions |
| Minimal Memory | Limit context retention and scope |
| Minimal Trust | Verify all inputs and outputs |

---

## Quick Reference Matrix

| Risk | Primary Threat | Key Control |
|------|----------------|-------------|
| ASI01 Goal Hijack | Objective manipulation | Input validation, human approval |
| ASI02 Tool Misuse | Unsafe tool usage | Sandboxing, argument validation |
| ASI03 Privilege Abuse | Credential misuse | Short-lived tokens, least privilege |
| ASI04 Supply Chain | Compromised components | Signed manifests, kill switches |
| ASI05 Code Execution | RCE via generation | Sandbox, review before execute |
| ASI06 Memory Poisoning | Context manipulation | Provenance tracking, segmentation |
| ASI07 Insecure Comms | Agent impersonation | mTLS, signed payloads |
| ASI08 Cascading Failures | Error propagation | Circuit breakers, isolation |
| ASI09 Trust Exploitation | Social engineering | Confirmations, audit logs |
| ASI10 Rogue Agents | Persistent compromise | Behavioral monitoring, kill switch |

---

## Companion Resources

- **State of Agentic Security and Governance 1.0**: Practical governance guide
- **Agentic Security Solutions Landscape**: Quarterly, peer-reviewed tool map
- **Practical Guide to Securing Agentic Applications**: Technical implementation guidance
- **OWASP FinBot CTF**: Capture-the-flag for practicing agentic security
- **Agentic Threats and Mitigations Document**: Threat-model-based reference

---

## Industry Adoption

- **Microsoft**: Agentic failure modes reference OWASP Threat and Mitigations
- **NVIDIA**: Safety and Security Framework references Agentic Threat Modelling Guide
- **GoDaddy**: Implemented Agentic Naming Service proposal in production
- **AWS & Microsoft**: Reference or embed Agentic Threats and Mitigations

---

## References

- [OWASP Top 10 for Agentic Applications](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/)
- [OWASP GenAI Security Project](https://genai.owasp.org/)
- [Aikido Analysis](https://www.aikido.dev/blog/owasp-top-10-agentic-applications)
- [Palo Alto Networks Guide](https://www.paloaltonetworks.com/blog/cloud-security/owasp-agentic-ai-security/)
