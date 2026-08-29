# Spark Cockpit: state of the branch for review (BETA)

Branch `webapp`, never pushed. Everything below was built, run and validated
live on the reference box on 2026-08-28, with screenshots taken through a
CDP-driven Chromium at every step.

## Run it

```bash
python3 dashboard/cockpit.py            # dev: http://127.0.0.1:30090
bash dashboard/install-dashboard.sh     # or as a systemd service (sudo asked once)
```

Login = the server API key (`~/.config/qwen38/api-key`). The sudoers allowlist
(nine exact systemctl lines, visudo-checked before install) is only needed for
the unit start/stop buttons; everything else runs unprivileged.

## What works today (all validated on screen)

- Live panels over SSE (1 s) with polling fallback and per-panel failure
  isolation: unified memory (the real GB10 gauge), CPU, GPU power/temp/
  processes (with the documented GB10 quirks), serving engine identity
  (model, revision, quant, context, speculative, backends, radix cache),
  live requests (running/waiting/tokens/accept length), services with
  start/stop buttons, containers, repo state.
- Engine chip states: healthy / booting / down. "Booting" is inferred from
  health-down plus a container burning CPU: a journald-silent weight load is
  not an outage (field lesson learned the hard way).
- Actions with NASA rails: fixed-argv registry, closed-enum validation,
  CSRF, single-job lock, append-only audit log, confirm modal showing the
  exact command, live job log streaming. Security paths tested: 403 without
  CSRF, 400 out-of-enum, isolated 502 while the engine boots.
- Inventory panel: the uninstall --list scan rendered (units, backups,
  config, legacy files, images by tag AND digest, checkpoints with sizes,
  the PLE backing file).
- Live logs panel: container logs and keepalive journal tail.
- opencode card with the one-command handoff.
- Login page, house palette light/dark, BETA badge, canvas sparklines,
  zero external dependencies anywhere (stdlib backend, vanilla frontend).
- Hardening: CSP + nosniff + no-referrer on every response, login rate
  limiting (5 tries per minute per address, audited), tested live
  (5x403 then 429, lockout applies to the right key too).
- End-to-end action demo performed IN the UI: Run smoke clicked, confirm
  modal, canary through the keepalive proxy, audit line `smoke ok:true`.

## Deliberately not done yet (needs your call or the next iteration)

- The atomic self-update engine for the app itself (release dirs + rollback):
  designed in DESIGN.md, waiting until the branch has a remote to update from.
- Structured per-request feed (parsing proxy lines into a table): the raw
  journal view covers it today.
- Bench runner and canary/needle buttons in the UI (backend patterns exist).
- LAN exposure flag + HTTPS story (localhost-only today by design).

## Field notes from building it

The cockpit caught real things while being built: a broken containers
collector (docker stats with an absent name), the display:grid vs hidden
modal bug, the engine restart of 17:34 whose clean-stop origin the tripwire
now watches for, and the boot-is-not-a-freeze lesson now encoded in the UI.


## Etape 1 livree: machine a etats + orchestrateur (29/08, nuit)

Validee par un test de torture REEL (stop puis start du 176B via l'API du
cockpit, boot complet observe en direct dans un navigateur pilote).

Ce que l'etape apporte, tout verifie a l'ecran ou par sonde:
- Etats explicites par moteur avec barre de progression par etape (pilules
  init/weights/draft/KV/graphs/warmup, shimmer, elapsed vs ETA apprise).
- Verrou anti-deux-moteurs applique DES DEUX COTES: bouton start grise avec
  la raison imprimee, ET refus 409 serveur avec les memes raisons.
- Avertissements honnetes: stop du flash en plein boot = alerte table PLE
  dans la modale; stop d'un moteur pret = alerte clients :30001.
- Fil d'evenements (transitions d'etats, jobs) pousse en SSE.
- Apprentissage des durees de boot (buckets rebuild separes, mediane /12),
  garde anti-fausse-mesure (activation temoin obligatoire).

Bugs REELS attrapes par le test (tous corriges + tests de regression):
1. parse sans marqueur pretendait toutes les etapes faites (queue de log
   d'un serveur mur = bruit decode pur).
2. docker logs parle sur stderr: le parseur et la telemetrie decode
   lisaient du VIDE depuis le debut (run() stdout-only).
3. le cockpit redemarre face a un moteur chaud enregistrait son uptime
   comme duree de boot (garde temoin ajoutee, historique purge).
4. chaque restart du service deconnectait tout le monde (secret HMAC en
   memoire): secret persiste 0600, session prouvee survivante.

Decouverte niveau REPO (backlog v1.6, branche main): systemctl stop laisse
l'unite en failed car le conteneur meurt en SIGKILL (exit 137); fix =
SuccessExitStatus=137 143 dans les templates d'unites, a valider par un
vrai cycle stop avant push.

Backlog court terme: persister le fil d'evenements (process-local
aujourd'hui), page-iser les logs, refonte design (etape 2).

## Etapes 2, 3 et 5 livrees (29/08, nuit, suite)

Etape 2, refonte design totale (verdict precedent: esthetique rejetee):
- Nouveau langage: fond encre a lueurs radiales, cartes verre en degrade,
  chips lumineuses, jauges en degrade, entree en cascade des cartes, point
  live qui bat, chiffres tabulaires. Sombre d'abord, clair via les memes
  tokens, prefers-reduced-motion respecte.
- Panneau moteurs promu en hero pleine largeur (la reponse au premier
  coup d'oeil), actions pleine largeur en bas. Login accorde.
- Methode: le bloc <script> est reste OCTET-IDENTIQUE (zero regression
  logique possible); les deux themes rendus et verifies en navigateur.

Etape 3, registre + veille upstream:
- /api/registry: scan du cache HF (metadata seulement, blobs dedupliques
  par inode), pins extraits generiquement des scripts du repo (le parseur
  a decouvert DRAFT2_REV tout seul), chaque revision classee
  pinned/stray/unmanaged, images moteurs docker. Cache 5 min.
- /api/upstream: chaque pin compare au main HF + tag local vs derniere
  release GitHub; cache 1 h; toute panne distante degrade en "offline"
  ligne par ligne. Premier run: il a affiche tout seul nos deux TODO v1.6
  (stock 27B "moved" = revert README connu; DSpark "moved" = la v2).
- UI: panneau Registry pleine largeur (revisions, tailles logiques et
  disque physique, chips de statut, images) + sous-panneau Upstream.

Etape 5 (partiel, les morceaux testables ce soir):
- Timeline d'evenements persistante (jsonl borne, rechargee au demarrage).
- Suite smoke HTTP: 17 assertions vivantes en une commande
  (dashboard/tests/smoke-http.sh): auth, CSRF, enums fermes, gate 409
  anti-deux-moteurs, anti-traversal statique, en-tetes. 17/17 vert.

Reste a construire (par choix, pas par oubli):
- Moteur de recipes custom (etape 4): le prochain grand chantier; la base
  (etats, gates, registre) est exactement ce qu'il lui fallait.
- Auto-update atomique de l'app: bloque tant que la branche est locale
  (il faut un remote pour verifier/telecharger des versions).
- Exposition LAN + HTTPS; actions de reclaim (suppression des revisions
  stray) avec double confirmation.

## Ceintures anti-blocage (29/08, journee autonome)

Cas de terrain du matin: ordonnanceur SGLang en boucle (R, 100 % CPU) pendant 9 h
avec /health a 200 et /get_load a 0 requete; le cockpit affichait « healthy ».

- Sonde de vraie generation (2 tokens, thinking off) toutes les 90 s, UNIQUEMENT
  quand un moteur se dit pret, qu'aucun job ne tourne et qu'AUCUNE requete n'est
  en cours ou en attente (sinon la sonde ferait la queue derriere une longue
  generation legitime; vu en vrai pendant un prefill de 140k tokens).
- Deux routes de decision (lifecycle.decide_wedge, pure, testee):
  idle = 0 requete et 3 sondes consecutives en echec; occupee = requetes en
  cours mais aucune ligne Prefill/Decode depuis 300 s. Jamais melangees.
- Etat « wedged » (rouge), compte comme occupe pour les gates; audit; autoheal
  (COCKPIT_AUTOHEAL=1 par defaut, refroidissement 30 min) qui redemarre l'unite
  via le chemin d'action audite.
- 43 tests unitaires verts, smoke HTTP 17/17.

## Passe de modernisation UI (29/08, apres-midi)

- Identite ancree dans l'objet (graphite du chassis, or champagne du Spark,
  vert NVIDIA), theme clair = papier chaud. Cascade d'entree retiree.
- Signature: le RESERVOIR KV en bande pleine largeur (tokens tenus / capacite
  reelle / tick « un prompt seul » / fenetre 262K hachuree = ce qui ne tient
  jamais). Jauge de pool normalisee aussi dans Live requests, avec phrase
  d'explication qui change au-dela de 70 %.
- Pilule « un coup d'oeil » dans l'en-tete (lane, etat, pool %, requetes).
- Feed des requetes (25 dernieres vues par le proxy: client, chemin, taille,
  duree, issue) et action « Abort all » (generations orphelines).
- Hysteresis de l'etat ready (3 rates + aucune ligne de progression) apres un
  battement ready/starting observe sous prefill de 140k.
- Plancher qualite: focus visible, Escape ferme la modale, mobile 390 px sans
  defilement horizontal (mesure: scrollWidth == clientWidth), squelettes sur
  tous les « ... », regions aria-live pour le journal d'evenements.

- Lighthouse (snapshot desktop, 29/08 13:06): accessibilite 100, bonnes pratiques 100, SEO 100, agentic browsing 100, 36 audits passes, 0 echec. CSP: script-src 'self' (scripts servis en fichiers).
- Action « diagnostics bundle »: tar.gz dans ~ avec journaux, logs conteneurs, nvidia-smi, unites, server-info (cle masquee), launcher (cle masquee), etat cockpit; pour les rapports d'issue.
- Hysteresis « ready » mesuree: 25 battements ready/starting entre 12:50 et 12:52 sous
  prefill 140k avant le correctif, 0 depuis 13:00 sous la meme charge (aiguilles).
- Sonde de generation: uniquement si 0 requete ET aucune ligne de progression depuis 60 s
  (entre deux requetes consecutives la charge lisait brievement zero).
- Pool guard (13:25): 2e blocage du moteur reproduit en conditions controlees (prefill d'un prompt
  ~93k termine a 89 % de pool apres 4 prompts 80k caches, aucun decode ensuite) et ATTRAPE par
  l'autoheal en 5 min (audit: wedge progress_age 302 s -> restart). Le cockpit flush maintenant le
  radix cache quand le moteur est idle depuis 3 s et que la derniere lecture du pool depasse 60 %
  (ou 50 % des slots mamba). Meme famille qu'upstream sglang #30314. Ligne « Mamba state slots »
  ajoutee a Live requests. COCKPIT_POOL_GUARD=0 pour desactiver.
- Forensique au blocage (13:50): wrapper root sans argument `/usr/local/bin/qwen38-pyspy-scheduler`
  (installe par install-dashboard.sh, ligne sudoers dediee) que le cockpit lance des qu'il declare
  un moteur « wedged »: piles Python du scheduler (py-spy, non bloquant) dans
  ~/.config/qwen38/wedge-<ts>.txt AVANT tout restart. `COCKPIT_AUTOHEAL_GRACE` (s) retarde le
  restart pour laisser le temps d'une autopsie manuelle (600 s sur la box de reference via drop-in
  systemd, 0 par defaut). Pool guard: se tait sur « pending requests » (400) et purge ses lectures.
- Sessions: 12 h glissantes (le navigateur de test s'est retrouve sur /login a 13:50 apres un
  login a 00:53: comportement voulu, pas un bug).
- Correctif critique (14:33): le patch « grace » avait place audit, dump et autoheal dans la branche
  NON figee (dumps toutes les 2 s en service normal, aucun autoheal pendant le blocage de 14:15).
  La decision est maintenant une fonction pure (`lifecycle.wedge_plan`, 5 tests) que le collecteur
  applique. Evidence native du blocage (gdb): thread principal du scheduler dans zmq_msg_recv/poll,
  boucle d'evenements qui spinne avec une requete en file jamais admise.
