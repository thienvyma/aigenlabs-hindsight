# AigenLabs Hindsight Fork

This repository is an AigenLabs-owned source copy of Hindsight for the Business
OS memory runtime.

Upstream base:

- Repository: `https://github.com/vectorize-io/hindsight`
- Tag: `v0.8.4`
- Commit: `92f433c90409636804c0797071a4abbe141f76c5`

Local AigenLabs patch:

- `hindsight-all/hindsight/embedded.py` accepts `codex_home` and maps it to
  `CODEX_HOME`.
- `hindsight-embed/hindsight_embed/daemon_embed_manager.py` forwards
  `CODEX_HOME` into the daemon subprocess environment.

Purpose:

- Keep Business OS Hindsight runtime source under AigenLabs control.
- Allow AigenLabs to build/release a pinned runtime without depending on future
  upstream package availability or behavior drift.
- Preserve native Hindsight behavior except for the bounded Codex OAuth home
  bridge needed by AigenLabs Business OS memory.
