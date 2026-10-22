# Prototypes du plan d'amélioration du 2026-10-21

Le plan qui explique, mesure et ordonne tout ceci est
[`../2026-10-21-plan-amelioration.md`](../2026-10-21-plan-amelioration.md) ; son annexe C dit, pour
chaque fichier, ce qu'il fait, comment il a été testé et comment l'adopter.

Rien ici n'est lu par la pile ni chargé par Claude Code tant que rien n'est déplacé : le dossier
s'appelle `claude/` et non `.claude/`, et le CLAUDE.md proposé s'appelle `CLAUDE.project.md`.
Les outils de `tools/` trouvent la racine du dépôt par git, et tournent donc aussi d'ici :

    python3 docs/reviews/2026-10-21-prototypes/tools/lanegen.py check
    python3 docs/reviews/2026-10-21-prototypes/tools/anchor-ratchet.py
    python3 docs/reviews/2026-10-21-prototypes/tools/affected-tests.py select

Commencer par `bash docs/reviews/2026-10-21-prototypes/claude/inventory.sh` (section 2 du plan).
