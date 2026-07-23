# Microsoft Foundry Agent Types — Quick Reference

## 1. Agent Builder / Prompt Agent vs Hosted Agent

Microsoft Foundry supports a spectrum from **declarative Prompt Agents** to **full-code Hosted Agents**.

| Agent Builder / Prompt Agent | Hosted Agent |
|---|---|
| Mostly declarative | Full-code approach |
| Configure **model + instructions + tools** | Write your own agent/orchestration code |
| Foundry hosts and runs the agent | Foundry hosts your packaged application/container |
| No application code or container required | You manage the agent code and framework |
| Fast to prototype and iterate | Maximum customization and runtime control |
| Best for straightforward agent scenarios | Best for complex workflows and custom orchestration |
| Built in Agent Builder / portal / SDK | Can use Microsoft Agent Framework, LangGraph, Semantic Kernel, or custom code |

### Mental model

```text
Prompt Agent
= Model + Instructions + Tools
= Declarative
= Foundry manages the runtime

Hosted Agent
= Your Agent Code + Framework + Dependencies
= Full code
= Packaged/deployed to Foundry
```

A **Prompt Agent** is the lightweight option when you mainly need to define agent behavior and attach tools.

A **Hosted Agent** is appropriate when you need control over orchestration, application logic, frameworks, dependencies, or multi-agent workflows.

**Reference:**  
https://learn.microsoft.com/en-us/azure/foundry/concepts/choose-build-approach

---

## 2. Local Prompt Agent vs Foundry Prompt Agent

These are **not two different agent architectures**.

They are two locations/modes for the same **Prompt Agent** concept:

```text
Prompt Agent
├── Local
│   └── Stored/worked on in the local VS Code development environment
│
└── Foundry
    └── Saved/published to the Microsoft Foundry project
```

| | Local Prompt Agent | Foundry Prompt Agent |
|---|---|---|
| Definition | Instructions + model + optional tools | Instructions + model + optional tools |
| Location | Local development environment | Microsoft Foundry project |
| Foundry-hosted | No | Yes |
| Shared through Foundry project | No | Yes |
| Foundry-side versioning | No | Yes — saving changes creates a new version |
| Foundry-managed history/resources | No Foundry-side persistence | Yes — full history and resource management |
| Best for | Local experimentation and prototyping | Shared/cloud-managed agent development |

### Agent Builder workflow

Agent Builder can work with both local and Foundry-hosted Prompt Agents.

Typical flow:

```text
Create Prompt Agent in Agent Builder
            |
            v
      Work/Test Locally
            |
            v
      Save to Foundry
            |
            v
Prompt Agent stored in Foundry project
      + versioning
      + project-level availability
```

The Agent Builder Prompt Agent switcher can show both **local agents** and agents **hosted in Foundry**.

**References:**  
https://code.visualstudio.com/docs/intelligentapps/create-agents  
https://code.visualstudio.com/docs/intelligentapps/agentbuilder

---

## Key Takeaway

There are **two separate dimensions**:

```text
1. HOW the agent is built
   ├── Prompt Agent    → declarative
   └── Hosted Agent    → code-based

2. WHERE a Prompt Agent lives
   ├── Local           → local development environment
   └── Foundry         → Microsoft Foundry project
```

So:

```text
Prompt Agent vs Hosted Agent
        ≠
Local Prompt Agent vs Foundry Prompt Agent
```

The first comparison is about **architecture and development model**.

The second comparison is about **where a Prompt Agent is stored/hosted and managed**.
