---
name: ui
description: Streamlit UI rules for V1 compatibility and V2 live monitoring.
---

# UI Skill

V1 must continue to support:
- CSV/DataFrame upload
- configuration
- batch execution
- result inspection
- AI explanations
- history/artifacts

V2 adds:
- source connection status
- worker status
- live event/change feed
- processed/pending counts
- latest checkpoint
- significance/hold state
- evidence details

Rules:
- UI is not the real-time worker.
- Do not put long-running ingestion loops in Streamlit.
- Do not expose service-role credentials.
- Prefer explicit connection/error states.
