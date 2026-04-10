# GateKeeper

**AI-Powered SDLC Gate — Security + Cost Intelligence on Every Pull Request**

GateKeeper is a GitHub Actions-based tool that analyses every PR for:
- **OSS vulnerabilities** via the Black Duck REST API
- **Cloud cost impact** via the AWS Pricing API (Kubernetes) and Infracost CLI (Terraform)
- **AI-synthesised verdict** (BLOCK / WARN / APPROVE) posted as a PR comment via Claude

---

## Architecture

```
PR opened / updated
        │
        ▼
  GitHub Actions (gatekeeper.yml)
        │
        ├──► pr_parser.py          — classify changed files
        │
        ├──► blackduck/            — async, parallel with cost
        │      ├── manifest_parser.py   — extract deps from package.json, pom.xml, etc.
        │      └── client.py           — Black Duck REST API, CVSS scores, licenses
        │
        ├──► cost/                 — async, parallel with BD
        │      ├── kubernetes.py       — resource requests/limits → AWS Pricing API
        │      └── infracost.py        — Terraform → Infracost CLI
        │
        ├──► ai/synthesizer.py     — Claude (claude-sonnet-4-6) verdict + markdown
        │
        └──► github/comment_bot.py — post/update PR comment
```

---

## Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.11+ | Core runtime |
| Infracost CLI | latest | Terraform cost estimation |
| AWS credentials | — | Pricing API read + CloudWatch |
| Black Duck Hub | — | OSS scan endpoint |
| Anthropic API key | — | Claude AI synthesis |

---

## Setup

### 1. Clone & install dependencies

```bash
git clone https://github.com/your-org/gatekeeper.git
cd gatekeeper
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Install Infracost CLI

```bash
curl -fsSL https://raw.githubusercontent.com/infracost/infracost/master/scripts/install.sh | sh
infracost auth login   # or: infracost configure set api_key <key>
```

### 3. Configure environment variables

```bash
cp .env.example .env
# Edit .env with your real credentials
```

### 4. Add GitHub Actions secrets

Go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret name | Description |
|-------------|-------------|
| `BD_API_URL` | Black Duck Hub URL |
| `BD_API_TOKEN` | Black Duck API token |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `AWS_ACCESS_KEY_ID` | AWS access key |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key |
| `AWS_REGION` | AWS region (default: `ap-south-1`) |
| `INFRACOST_API_KEY` | Infracost API key |

`GITHUB_TOKEN` is provided automatically by GitHub Actions.

### 5. Open a test PR

Create a PR that modifies one of: `package.json`, `requirements.txt`, a Kubernetes YAML, or a `.tf` file. GateKeeper will fire and post a comment within ~2 minutes.

---

## Local development

```bash
# Set env vars (or load from .env)
export $(cat .env | xargs)

# Fake the PR context
export PR_NUMBER=1
export REPO_FULL_NAME=your-org/your-repo
export BASE_SHA=$(git rev-parse HEAD~1)
export HEAD_SHA=$(git rev-parse HEAD)

cd src
python main.py
```

---

## Verdict logic

| Verdict | Trigger |
|---------|---------|
| 🔴 **BLOCK** | CRITICAL CVE (CVSS ≥ 9.0) **or** cost delta > $500/mo |
| 🟡 **WARN** | HIGH CVE (CVSS 7–8.9) **or** cost delta $100–$500 **or** copyleft license |
| 🟢 **APPROVE** | No HIGH/CRITICAL CVEs, cost delta < $100, no copyleft |

---

## Project structure

```
.
├── .github/
│   └── workflows/
│       └── gatekeeper.yml        # GitHub Actions workflow
├── src/
│   ├── main.py                   # Pipeline orchestrator
│   ├── pr_parser.py              # PR diff → categorised file lists
│   ├── blackduck/
│   │   ├── client.py             # Black Duck REST API client
│   │   └── manifest_parser.py    # package.json / pom.xml / go.mod / etc.
│   ├── cost/
│   │   ├── kubernetes.py         # K8s manifest → AWS Pricing API cost delta
│   │   └── infracost.py          # Terraform → Infracost CLI cost delta
│   ├── ai/
│   │   ├── synthesizer.py        # Claude API call + verdict parsing
│   │   └── prompts.py            # System + user prompt templates
│   └── github/
│       └── comment_bot.py        # Post/update PR comment via PyGithub
├── .env.example                  # Environment variable reference
├── requirements.txt              # Python dependencies
└── README.md
```

---

## Contributing

Stages to extend:
1. **Helm values analysis** — detect replica/instance-type changes in `values.yaml`
2. **CloudWatch metrics** — emit `GateKeeper/Verdict`, `GateKeeper/Duration` metrics
3. **Verdict history** — track BLOCK→WARN→APPROVE progression across PR iterations
4. **License policy** — configurable allowed/blocked license list via `gatekeeper.yml`
