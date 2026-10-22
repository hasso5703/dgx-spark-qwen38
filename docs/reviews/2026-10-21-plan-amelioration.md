# Plan d'amélioration de dgx-spark-qwen38 : cinq recommandations mesurées

Rédigé les 2026-10-21 et 22 par une session Claude Code dans le cloud (claude.ai/code), à la demande de
Hasan, pour l'agent Claude Code qui développe et maintient ce dépôt sur le DGX Spark. Base
analysée : `main` à `e07dd47` (CHANGELOG v1.37.7), historique complet (898 commits, 121 tags).

## 0. À lire d'abord : d'où vient ce document, et ce qu'il ne sait pas

**Ce que j'ai eu.** Le dépôt GitHub et son historique, l'API GitHub (issues, pull requests, runs
de CI), le web, et un conteneur Linux jetable : x86_64, 4 vCPU, 15 Go de RAM, Python 3.13, sans
GPU, sans systemd comme PID 1, sans démon Docker.

**Ce que je n'ai pas eu, et que toi, agent local, tu as.** Rien de la machine :
- ni `~/.claude/` (ton CLAUDE.md global, ton auto-mémoire `~/.claude/projects/.../memory/`, tes
  skills, tes sous-agents, tes hooks, tes serveurs MCP) ;
- ni la box (le GB10, les units installées, le cache HF, les images Docker, les journaux) ;
- ni les rapports du scanner de vulnérabilités GLM, ni les transcripts de tes sessions.

Hasan m'a précisé que tu travailles ainsi : sous-agents de review (skill code-review) en fin de
travail, puis tests et CI locale avant publication, le tout cadré par ta mémoire globale et
l'auto-mémoire que tu tiens toi-même. Je n'ai pu lire aucun de ces fichiers. Tout ce qui touche à
ta configuration locale est donc une **hypothèse à vérifier**. La section 2 te donne un script
d'inventaire pour le faire avant d'appliquer quoi que ce soit.

**Comment lire chaque chiffre.** Chaque affirmation porte l'une de ces marques :
- **[historique]** : calculé sur l'historique git ou l'API GitHub, avec la commande en annexe ;
- **[code]** : vérifiable en lisant le dépôt, avec le fichier et la ligne ;
- **[mesuré ici]** : mesuré dans le conteneur décrit plus haut. Le nombre est réel, mais le
  conteneur n'est pas la box : les durées absolues diffèrent, les proportions sont ce qui compte ;
- **[à valider sur la box]** : une hypothèse raisonnée que seule la box, ou toi, peut confirmer.

**Ce qui a déjà été proposé, et pourquoi ce document existe.** Une revue cloud du 2026-10-18
(PR #52, fermée sans merge) proposait déjà un registre des lanes, une batterie de conformité, un
scaffold, une commande de qualification, un fichier de pins, une CI plus rapide et un canal de
release pour `get.sh`. Ses correctifs F1 à F11 sont entrés dans v1.36.3. **Aucune de ses sept
propositions de design n'a été réalisée.** Elles ne figurent pas dans ROADMAP.md, son document
(`docs/reviews/2026-10-18-cloud-review.md`) n'est pas dans `main`, et la lane suivante
(Flash FP4-Local, v1.37.0) a été câblée à la main dans 35 fichiers partagés **[historique]**. Je ne
répète donc pas ces propositions : j'essaie d'expliquer pourquoi elles n'ont pas pris (section 1),
et je fournis pour chacune **une première étape sûre, prototypée et testée ici**, plus les mesures
qui manquaient pour décider.

**Sur les commits de cette branche.** D'après la revue du 2026-10-18, tes contrôles pre-push
refusent les commits rédigés par Claude ou portant une ligne `Claude-Session`. Ceux de cette
branche en portent. Reprends le contenu dans tes propres commits plutôt que de fusionner la
branche.

## 1. Synthèse

Ce document ne dresse pas la liste de ce qui marche. Il cherche où le processus actuel coûte du
temps sans en rendre, et où il laisse passer des défauts. Cinq recommandations, chacune avec sa
preuve, sa première étape prête et sa mesure de succès :

| # | Recommandation | Le problème, en chiffres | Ce qui est fourni et testé ici | Effort |
|---|---|---|---|---|
| 1 | **Une échelle de vérification**, de la milliseconde à la CI complète | la seule vérification d'ensemble prend 45 min sur la box et 42 min (médiane) sur GitHub ; deux étapes font 72 % de la CI ; `test_text_lane.py` (9 330 séquences, sept lanes sur huit) y tourne quatre fois | hook de portes statiques (50 à 120 ms) ; `affected-tests.py` (rejeu de 100 commits : 1,4 min en médiane, 6,0 min au 90e centile avec le correctif 1, au lieu de 22 min pour toute la suite) ; deux patchs sur les deux étapes lourdes, la CI complète à 41 min ici avec les correctifs 1 et 2 (mesuré : 51 % de moins) ; un test instable et une fuite d'environnement trouvés, et corrigés | 2 à 3 jours |
| 2 | **Une seule liste des lanes**, le reste généré ou vérifié | 64 listes dans 20 fichiers ; FP4-Local a demandé 35 fichiers partagés câblés à la main ; blocs vLLM de `switch-model.sh` identiques à 73-77 % | `lanes.toml` et `lanegen.py check` : 27 listes sur 27 d'accord, sans rien changer | phase 1 : 1 à 2 jours |
| 3 | **Des tests qui vérifient le comportement, pas le texte** | au moins 489 ancres de découpage dans 89 fichiers de test ; les 3 refactorings neutres essayés cassent chacun un test ou une étape de CI | expérience de refactorings neutres, porte à cliquet `anchor-ratchet.py` (testée) | 1 heure, puis au fil de l'eau |
| 4 | **Ce qui arrive chez l'utilisateur est ce qui a été validé** | `main` en avance sur sa CI 21,7 h en 8 jours, et rouge jusqu'à 3,5 h ; 232 paquets non épinglés dans le venv image ; 7 rapports de bugs externes sur 13 viennent de box neuves | job `promote` du canal `stable` (zizmor : 0), lock hashé démontré | 1 à 3 jours |
| 5 | **Ton savoir-faire dans le dépôt, les règles dures dans des hooks** | aucun fichier `.claude` versionné ; auto-mémoire locale à la box ; « found in review » sur 645 lignes ; deux correctifs v1.37.x livrent ce qu'une review a trouvé après la release | CLAUDE.md de projet, 3 skills, 1 sous-agent, 2 hooks, inventaire | 1 jour |

### Pourquoi les propositions du 2026-10-18 n'ont pas pris (mon hypothèse, à confronter à ta mémoire)

1. **Elles n'ont jamais atteint un endroit qui pilote le travail.** Elles vivaient dans une PR
   fermée ; ni ROADMAP.md ni `main` ne les contiennent **[code]**. Je ne sais pas si ton
   auto-mémoire les a retenues : vérifie-le.
2. **Elles n'avaient pas de première étape sûre.** La revue elle-même les disait « each larger
   than one safe commit ». Ici, chaque recommandation commence par un commit qui ne change aucun
   comportement et que la CI peut vérifier (par exemple `lanegen.py check`, d'accord avec le dépôt
   tel qu'il est).
3. **Le coût d'un refactoring est caché dans les tests.** Au moins 489 ancres découpent les
   scripts par leur texte : factoriser `switch-model.sh` oblige à réécrire des dizaines de tests,
   à 45 minutes de CI par essai. Copier un bloc coûte moins cher, d'où les blocs vLLM identiques à 73-77 %
   **[code, mesuré ici]**.
4. **Le flux de fonctionnalités passe devant.** Depuis la revue (intégrée en v1.36.3), neuf
   releases, de v1.36.4 à v1.37.7, ont livré une lane et les correctifs d'une box neuve
   **[historique]**. Chaque nouvelle lane ajoutée à
   l'ancienne augmente le coût de la migration suivante.

D'où l'ordre proposé (section 9) : d'abord ce qui accélère chaque itération (1 et 5), puis ce qui
rend le refactoring bon marché (3), puis la migration elle-même (2), et en parallèle ce qui protège
les utilisateurs (4).

## 2. Phase 0 : ton inventaire, avant tout le reste (30 minutes)

Je n'ai vu aucun de tes fichiers locaux. Avant d'appliquer une recommandation, fais l'inventaire
de ce que tu sais déjà et de l'endroit où tu le gardes :

    bash docs/reviews/2026-10-21-prototypes/claude/inventory.sh > /tmp/inventory.txt

Le script est en lecture seule. Il affiche des noms, des tailles, des comptes et la première
ligne de chaque description, jamais la valeur d'un réglage, d'une variable d'environnement ou
d'un jeton. Il couvre : les fichiers d'instructions (CLAUDE.md global et de projet,
`.claude/rules/`), l'auto-mémoire du projet, les skills, les sous-agents, les hooks (événement et
matcher seulement), les hooks git du clone (dont les contrôles pre-push cités le 2026-10-18), les
serveurs MCP, et ce que le dépôt versionne de tout cela (aujourd'hui : rien).

Range ensuite chaque élément dans l'une de ces trois cases :

| Ce que c'est | Où ça doit vivre | Pourquoi (documentation officielle de Claude Code) |
|---|---|---|
| Une règle qui doit **toujours** tenir (pas de tiret cadratin, stdlib seulement, jamais `sudo ./install.sh`, CI complète avant un tag) | un hook ou une porte de CI | « Claude treats them as context, not enforced configuration » ; pour bloquer une action quoi que décide Claude, la doc renvoie à un hook `PreToolUse` ([memory](https://code.claude.com/docs/en/memory)) |
| Un savoir du projet (où sont les pins, comment on ajoute une lane, le protocole de mesure, les classes de défauts connues) | le dépôt : `CLAUDE.md` de projet (moins de 200 lignes), `.claude/rules/` à portée de chemins, `.claude/skills/`, `.claude/agents/` | « Auto memory is machine-local » et « Files are not shared across machines or cloud environments » ; un fichier versionné se relit, se revoit en review et survit à la box ([memory](https://code.claude.com/docs/en/memory), [skills](https://code.claude.com/docs/en/skills), [sub-agents](https://code.claude.com/docs/en/sub-agents)) |
| Une préférence de Hasan ou un fait de cette box seule (chemins locaux, état du réseau, habitudes) | ton auto-mémoire, ou `CLAUDE.local.md` (ignoré par git) | c'est ce pour quoi l'auto-mémoire est faite |

Deux contrôles de plus, rapides :
- `MEMORY.md` : seules ses 200 premières lignes ou ses 25 premiers Ko sont chargés au démarrage
  ([memory](https://code.claude.com/docs/en/memory)). Ce qui dépasse n'existe pas pour toi en
  début de session.
- `/doctor prompt-audit` repère les consignes contradictoires ou périmées entre CLAUDE.md, règles,
  skills et sous-agents ([memory](https://code.claude.com/docs/en/memory)).

Ce qui ressort de ton inventaire décide de la forme exacte des recommandations 3 et 5. Si une
skill ou un hook de ce document existe déjà chez toi sous un autre nom, garde le tien et prends ce
qui manque.

## 3. Recommandation 1 : une échelle de vérification, de la milliseconde à la CI complète

### Le problème, mesuré

- **Une seule vérification d'ensemble, et elle est longue.** Une CI locale complète prend 45 minutes
  sur la box de référence (TESTING.md, mesure du 2026-10-18) ; sur GitHub, la médiane des 80
  derniers runs sur `main` est de 42 minutes, et le maximum de 81 **[historique]**. L'outil fin
  qui existe, `./ci-local.sh <fragment>`, filtre les étapes de CI par leur nom : il ne sait pas
  quels tests un changement concerne, c'est à toi de le deviner.
- **Où passe le temps [mesuré ici].** La CI complète a pris 84 minutes (5 021 s) dans ce conteneur (annexe A).
  Deux étapes en font 72 % :

| Étape de `ci.yml` | Durée ici | Part du total |
|---|---:|---:|
| No test file in tests/ is left unrun | 2 404 s | 48 % |
| The offline suite touches nothing outside itself | 1 195 s | 24 % |
| Coverage floors, the root modules | 320 s | 6 % |
| Coverage floors (measured, per module) | 305 s | 6 % |
| Mutation score (do the tests catch a wrong operator?) | 261 s | 5 % |
| Cockpit unit tests (every tests/test_*.py, discovered) | 259 s | 5 % |
| The cockpit page works under a finger (headless browser, real taps, a cockpit of its own) | 108 s | 2 % |
| les 42 autres étapes | 170 s | 3 % |

- **Ce qui rend ces deux étapes longues [code, mesuré ici].**
  - « No test file in tests/ is left unrun » exécute tous les modules de `tests/` **dans un seul
    processus, l'un après l'autre** (40 minutes ici). C'est voulu : un module qui change l'état
    du processus pour les suivants s'y voit. Mais aucun parallélisme ne la raccourcit.
  - `tests/test_suite_isolation.py` relance chacun des modules de `tests/` dans un processus neuf,
    pour vérifier qu'aucun ne laisse l'environnement modifié, et il tourne dans cette étape comme
    dans l'étape hors ligne (20 minutes ici). Avec la couverture, chaque module de `tests/` tourne
    donc quatre fois par CI, cinq pour les 24 que la couverture mesure.
  - `tests/test_text_lane.py::test_every_sequence` énumère **9 330 séquences** de bascules
    (6 + 36 + 216 + 1 296 + 7 776), un processus bash chacune : 12 minutes seul sur une machine calme (17 au milieu des autres fichiers de l'étape hors ligne),
    et il fait partie de ces quatre exécutions. Le nombre de séquences croît comme `(N-1)^5` avec
    le nombre N de lanes : 19 607 avec la huitième, qu'il ignore aujourd'hui (section 5), 37 448
    avec une neuvième. Chaque lane ajoutée le double, à peu près.
  - Lancés chacun seul, quatre à la fois comme dans l'étape hors ligne, 220 fichiers de test ont pris 19 minutes, puis les 12 qui tournent seuls 3,1 minutes, soit 53 minutes de travail en série. Les deux plus longs : `tests/test_suite_isolation.py` (1 114 s) et `tests/test_text_lane.py` (1 017 s) ; le suivant, `tests/test_check_pins.py`, prend 165 s.
  - Les 12 fichiers qui prennent un numéro de port (`getsockname()[1]` puis fermeture de la socket)
    tournent en série après les autres, pour qu'un voisin ne reçoive pas le même port **[code]**.

### L'échelle proposée

| Barreau | Quand | Ce qui tourne | Durée |
|---|---|---|---|
| 0 | à chaque fichier écrit, automatiquement | `fast-checks.sh` : les portes statiques sur ce fichier | 50 à 120 ms **[mesuré ici]** |
| 1 | avant chaque commit | `affected-tests.py run` : les fichiers de test qui lisent ce qui a changé | 1,4 min en médiane ; au 90e centile 6,0 min avec le correctif 1 ci-dessous, 20 min sans **[mesuré ici]** |
| 2 | avant un push sur `main`, un tag, une release | `./ci-local.sh` complet (imposé par `guard-release.sh`) | 45 min aujourd'hui sur la box, 84 ici ; 41 min ici avec les correctifs 1 et 2 (mesuré : 51 % de moins) |
| 3 | avant une release mineure | les preuves sur la box (`--with-<lane>`) et la répétition d'une première installation (section 6.4) | selon les lanes |

**Barreau 1 : `affected-tests.py` [prototype, mesuré ici].** `map` lance chaque fichier de test
seul sous `strace -f` et enregistre tout ce que lui et ses processus enfants (bash, python, node)
ouvrent, testent ou listent dans le dépôt. Un fichier est sélectionné si lui-même a changé, si un
fichier qu'il lit a changé, ou si une entrée est apparue ou a disparu dans un répertoire qu'il liste
(un nouveau fichier, ou le nouveau répertoire qui le contient ; les tests qui demandent
`git ls-files` sont sélectionnés par tout ajout ou suppression). Tout ce que la carte ne peut pas
garantir sélectionne tout : pas de carte, une carte construite sur un commit qui n'est pas un
ancêtre de HEAD, un changement de `ci.yml`, de `ci-local.sh`, de l'outil lui-même ou de
`tests/testpy.sh`. `run` laisse `tests/test_suite_isolation.py` au barreau 2 : il relance toute la
suite, donc lit ce que lisent tous les autres, et serait sélectionné par presque tout changement ;
il vérifie la suite, pas un changement. Stdlib seulement ; strace n'est nécessaire qu'à `map`.

**[mesuré ici]** La carte des 232 fichiers s'est construite en 60 min sous strace (la machine
faisait tourner d'autres mesures),
dont 53 min pour `tests/test_text_lane.py` seul (ses 9 330 processus bash tracés) : avec le correctif 1
ci-dessous, ce coût disparaît. Rejouée sur les 100 derniers commits de `main` (le diff de chaque commit,
appliqué à la suite d'aujourd'hui ; durées tirées de la mesure par fichier, quatre fichiers à la fois) :

| Pour un commit de l'historique | Médiane | 75e centile | 90e centile | La suite entière |
|---|---:|---:|---:|---:|
| fichiers de test sélectionnés | 52 | 133 | 198 | 232 |
| durée de `run` | 1,4 min | 20 min | 20 min | 22 min |
| durée de `run`, avec le correctif 1 | 1,4 min | 5,7 min | 6,0 min | 6,9 min |

La médiane est basse, la queue ne l'est pas : 32 commits sur 100 touchent un fichier que
`tests/test_text_lane.py` lit (`lanes.sh`, `switch-model.sh`, `install.sh`...), et ce fichier pèse à lui seul
17 minutes dans cette mesure (12 seul). Le barreau 1 et le correctif 1 vont ensemble : la sélection ne paie
vraiment qu'une fois ce test réduit.

4 des 100 commits sélectionnent tout (ils modifient `ci.yml`) ; avec le correctif 1,
59 commits se vérifient en moins de 2 minutes et 75 en moins de 5 ; `test_suite_isolation.py`
aurait été sélectionné par les 100, d'où sa place au barreau 2.

Quatre modifications faites pour de vrai dans le clone de mesure, puis `run` chronométré sur la machine
calme ; la dernière colonne rejoue la même sélection avec la version réduite de `test_text_lane.py`
(50 s seule) :

| Modification | Fichiers lancés | `run`, mesuré | Avec le correctif 1 (estimé) |
|---|---:|---:|---:|
| une page de la doc (`docs/operations.md`) | 3 | 3 s | 3 s |
| une docstring du cockpit (`dashboard/lifecycle.py`) | 47 | 1,0 min | 1,1 min |
| un commentaire de la bibliothèque des lanes (`lanes.sh`) | 51 | 13 min | 50 s |
| un commentaire de switch-model.sh (`switch-model.sh`) | 46 | 13 min | 50 s |

Le rejeu surestime un peu : ses durées par fichier viennent d'une mesure à quatre fichiers à la fois,
où `test_text_lane.py` prend 17 minutes au lieu de 12 seul : pour les deux dernières modifications, il
aurait annoncé 17 minutes, la mesure en donne 13.

C'est la sélection par dépendances, que Facebook décrit comme sûre mais trop large (« It ends up
saying "yes, this test is impacted" more often than is actually necessary ») avant de lui ajouter
un modèle appris ([Predictive Test Selection, 2018](https://engineering.fb.com/developer-tools/predictive-test-selection/)).
Pour ce dépôt, la version simple suffit : elle ne sélectionne que des fichiers qui lisent ce qui a changé, et tout ce qu'elle ne peut pas garantir sélectionne tout ; sa sûreté se vérifie en lançant toute la suite contre des mutants d'une ligne, chaque fichier qui échoue devant être dans la sélection (la série lancée ici a été coupée par un redémarrage du conteneur, elle est à refaire sur la box), et la CI complète reste obligatoire
avant tout ce qui quitte la box.

**Barreau 2 : la CI complète, plus courte sans rien perdre.** Cinq correctifs, du plus rentable
au moins rentable :
1. **`test_text_lane.py`** : garder exhaustives les séquences jusqu'à quatre bascules (ce qui
   couvre tout aller-retour entre deux lanes annexes depuis une lane texte, la forme du défaut du
   2026-10-14), puis un échantillon de séquences plus longues (tirées au hasard avec une graine
   fixe, ou générées par hypothesis, déjà utilisé par `proxy_fuzz.py`), et tirer les lanes de
   `lanes.toml`. **[mesuré ici]** La version réduite (`patches/test-text-lane-reduced.diff`, sur les huit lanes : 399 séquences exhaustives et 300 tirées, et le test ciblé qui manquait) passe ses 22 tests en 50 s, contre 740 s pour les 21 de l'original sur sept lanes, chacun seul sur la même machine calme : 15 fois plus vite, en couvrant plus. Les deux étapes lourdes, rejouées en root comme la première fois avec les patchs des correctifs 1 et 2 :

| Étape | Avant | Après les correctifs 1 et 2 |
|---|---:|---:|
| No test file in tests/ is left unrun | 40 min | 11 min |
| The offline suite touches nothing outside itself | 20 min | 6 min |
| **la CI entière** | **84 min** | **41 min** |

Les autres étapes ne changent pas. En root, ces deux étapes échouent avant comme après (les installeurs refusent root, et quatre tests vérifient des permissions que root contourne) : la preuve que les patchs ne cassent rien vient des exécutions en utilisateur ordinaire, ci-dessous. Huit mutants de `lanes.sh` et des notes de `switch-model.sh` : les quatre que l'enchaînement des bascules peut voir sont attrapés dès la première ou la deuxième séquence, de **deux** bascules ; les quatre autres ne le sont par aucune des 19 607 séquences jusqu'à six bascules sur huit lanes (trois mesurés jusqu'au bout, le quatrième arrêté par la limite de deux heures d'une tâche de ce conteneur, et invisible au rejeu par construction : il ne démarre jamais deux lanes texte à la fois). L'énumération exhaustive ne les voit pas ; c'est le rôle des tests ciblés du même fichier, et deux de ces mutants, une note de Kolibri ou de Flash FP4-Local réduite au seul 27B, passaient **tout le fichier actuel**. Le patch ajoute le test qui manquait (`test_a_lone_flash_at_boot_is_noted_by_every_side_lane`) : il passe sur `main` et tue les deux. Enfin, l'énumération complète à huit lanes (19 607 séquences, dont les 10 277 qui passent par Flash FP4-Local et que le test actuel ne lance pas) a tourné jusqu'au bout sur ces mutants invisibles au rejeu, sans séquence fausse ; le premier ne change rien quand toutes les lanes texte peuvent démarrer, ce qui est le cas du rejeu : le code actuel tient donc la propriété pour la huitième lane, c'est le test qui ne la vérifiait pas.
2. **Le contrôle d'environnement de `test_suite_isolation.py`** : l'étape hors ligne installe déjà
   un `sitecustomize` (son audit des ports). Y ajouter la comparaison de `os.environ` entre le
   démarrage et `atexit`, écrite dans un journal que l'étape lit, fait le même contrôle pendant
   l'exécution unique de chaque fichier. La relance complète disparaît ; restent le test d'import
   et celui des fixtures de clé, 4,6 secondes ici pour les deux. **[mesuré ici]** La sonde nomme un fichier de test écrit pour laisser `UPSTREAM` changé, et pas un fichier qui remet l'environnement en place. Puis, sur la vraie suite et en utilisateur ordinaire, l'étape hors ligne patchée fait passer tous les fichiers (220 quatre à la fois en 276 s, puis les 12 seuls en 185 s), et la sonde signale **une fuite réelle** : `dashboard/tests/test_cockpit_logs_masked.py` laisse six variables `COCKPIT_*` changées, dont `COCKPIT_CONFIG_DIR`, qui pointe vers le dossier que son `tearDownClass` vient de supprimer, pour tous les modules qui le suivent dans l'étape « Cockpit unit tests », qui les exécute dans un seul processus. C'est la classe de défaut du 2026-09-24, et `dashboard/tests/test_env_isolation.py` existe pour elle ; mais il ne lance que le premier test de chaque module, et celui de ce module est dans une autre classe que celle qui pose les variables. `patches/cockpit-env-leak.diff` corrige les deux : la sonde du cockpit lance le premier test de chaque classe (elle échoue alors en nommant la fuite **[mesuré ici]**), et le module rend l'environnement comme les autres (elle passe). L'ancienne relance ne couvrait que `tests/` : la sonde couvre aussi `dashboard/tests/`.
3. **L'exécution commune de `tests/` en quatre processus** : quatre lots fixes de modules, chacun
   dans un seul processus, gardent l'essentiel de ce que cette étape attrape (un module qui
   change l'état du processus pour ceux qui le suivent dans son lot), et le compte « déclarés
   contre exécutés » se fait par lot.
4. **Les ports** : faire écouter les serveurs de test sur le port 0 et lire le port réellement
   attribué (ou passer la socket déjà liée au processus enfant), au lieu de `getsockname()[1]`
   suivi d'une fermeture. Les 12 fichiers rejoignent alors le lot parallèle.
5. **Les étapes indépendantes en parallèle** : `ci-local.sh` donne déjà à chaque étape son HOME et
   son TMPDIR ; un `-j 2` sur les étapes lourdes et sur GitHub, des jobs parallèles plutôt qu'un
   seul job séquentiel. Les runners standard d'un dépôt public sont gratuits et illimités, avec
   4 vCPU et 16 Go, et il existe des runners arm64 `ubuntu-24.04-arm`, l'architecture de la box,
   que la CI n'a jamais vue ([GitHub](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)).

**Avant de paralléliser : un test qui échoue sous charge [code, mesuré ici].**
`tests/test_proxy_engine_withholds_headers.py::test_the_request_is_dropped_past_max_silence` lit le
journal du proxy une seule fois, dès que le moteur factice a vu la connexion fermée ; or le proxy
n'écrit sa ligne de fin (`self._done(...)`, `keepalive-proxy.py:4602`) qu'après avoir fini la
réponse que le test vient de lire. Sous charge, la lecture passe avant l'écriture : l'étape hors
ligne relancée ici en utilisateur ordinaire a échoué sur ce test, puis le fichier a échoué
1 fois sur 5, et le test seul 1 fois sur 30, contre 0 sur 30 avec `patches/proxy-drop-test-race.diff`
(attendre la ligne avec la même échéance que la fermeture), en alternance et dans les mêmes
conditions. Le dépôt refuse d'appeler cela un flake (« the kind of failure that gets called a
flake and muted », `tests/test_suite_isolation.py`) : le correctif 5 le rendrait plus fréquent, à
corriger avant. Un second test l'est sans doute aussi : `tests/test_bench_agent_slope.py` a lu une
pente de 214 pour un plancher de 300 dans une étape à quatre fichiers à la fois, et passe seul
(20 fois sur 20 sous une charge modérée). Sa pente se calcule sur deux points et une croissance
d'une centaine de jetons, le cas que décrit le commentaire même de `bench-agent.py` (« a few
milliseconds of jitter read as a steep slope ») : lui donner une croissance plus grande.

**Gain attendu.** La CI complète n'est plus le prix de chaque modification, seulement celui de ce
qui sort de la box. Pour un commit courant, la vérification passe de 45 minutes à 1,4 min en médiane ; au 90e centile 6,0 min avec le correctif 1 ci-dessous, 20 min sans **[mesuré ici]**.

**Risques.** Une carte périmée sous-sélectionne : d'où la règle « carte d'un commit qui n'est pas
un ancêtre, donc tout », et la reconstruction de la carte à chaque CI complète réussie. Un test qui
dépend d'un état hors du dépôt (le réseau, l'horloge) n'est pas vu par la carte : c'est ce que la CI
complète du barreau 2 continue d'attraper. Réduire `test_text_lane.py` retire des séquences : la
mesure des mutants ci-dessus dit ce qu'on perd, à refaire si la logique de `lanes.sh` change de
forme (plus d'un enregistrement, par exemple).

## 4. Recommandation 2 : une seule liste des lanes, et tout le reste généré ou vérifié contre elle

### Le problème, mesuré

- **64 listes** de trois lanes ou plus, dans **20 fichiers** : shell, Python du cockpit,
  JavaScript de la page, templates d'units **[code]**. Je les ai comptées avec les fonctions
  mêmes de `tests/test_lane_lists_whole.py` (commande en annexe B). Ce test existe justement
  parce qu'une lane oubliée dans une liste « was a box left with no engine », et il embarque un
  tokenizer JavaScript d'environ 150 lignes pour retrouver ces listes **[code]**.
- **Chaque nouvelle lane modifie toutes les autres units** : `Conflicts=` et `After=` sont écrits
  à la main dans les deux sens, donc l'ajout de Flash FP4-Local a modifié les 9 templates
  existants, 2 lignes chacun, en plus du sien (`git diff --numstat v1.36.4..v1.37.0 --
  'qwen38-*.template'`) **[historique]**. Le coût croît en N².
- **Le câblage pèse plus lourd que la lane** : l'ajout de Flash FP4-Local, c'est 5 fichiers
  propres à la lane (+1 573 lignes : installeur, template, `ple-table.py`, profil seccomp, helper
  opencode) et **35 fichiers partagés** (+701 / -193) **[historique]**. Les quatre dernières lanes
  ont touché de 72 à 98 fichiers chacune (Kolibri 72, Bolt 85, Super VL 98, FP4-Local 84)
  **[historique]**.
- **Du copier-coller mesurable** : dans `switch-model.sh`, les blocs des lanes vLLM (Kolibri,
  Nemotron, Flash FP4-Local) sont identiques à **73 à 77 %** une fois les noms de lanes remplacés
  (`difflib`, annexe B) **[code]**. `oc-kolibri.py`, `oc-nemotron.py` et `oc-flash-local.py` ne
  diffèrent que par des données (nom, niveaux de raisonnement, modalités) **[code]**.
- **Le modèle de données du cockpit ne connaît pas les lanes** : `dashboard/recipes.py` déclare
  `LANES = ("27b", "flash")`, deux lanes sur huit, et `dashboard/static/js/base.js` tient une
  quinzaine de tables par lane ou par cible (`LANE_NAME`, `LANE_META`, `READY_DEFAULT`,
  `LANE_TARGETS`, `TARGET_NAME`, `TARGET_NOTE`, `LANE_INSTALL`, `LANE_RUNTIME`, `SIDE_LANE`,
  `*_CANNOT`...) **[code]**. `dashboard/registry.py` garde sa propre copie des dépôts épinglés
  (`PIN_MODELS`, « kept in lockstep with switch-model.sh »), la liste des installeurs où relire
  les pins (`PIN_FILES`) et une regex des images de chaque lane (`ENGINE_IMAGE_RE`) **[code]**.
- **Et la dérive est déjà là** : l'en-tête de `dashboard/sudoers-cockpit.template` parle de « the
  six serving units and the opencode web server » alors que neuf units de service (les huit
  lanes et le proxy), plus le serveur opencode, y ont leurs lignes start/stop/restart **[code]** ;
  `tests/test_text_lane.py` énumère sept lanes et ignore Flash FP4-Local (section 5) **[code]**.

Pour le produit visé (un « LM Studio du DGX Spark » où l'on sert un modèle en quelques clics),
c'est le point bloquant : tant qu'une lane est une trentaine de modifications dispersées, le
catalogue ne peut pas être une donnée que l'interface affiche et que l'installeur exécute.

### Ce que font les projets qui servent beaucoup de modèles

- **OME** (Open Model Engine, l'opérateur Kubernetes de l'équipe SGLang) sépare le modèle
  (`BaseModel`) du runtime qui le sert (`ServingRuntime`), et choisit le runtime à partir des
  métadonnées du modèle : « The model itself should drive the deployment »
  ([LMSYS, 2025-07-08](https://lmsys.org/blog/2025-07-08-ome),
  [docs](https://docs.sglang.io/ome/docs/concepts/serving_runtime)).
- **LM Studio** décrit chaque modèle et toutes ses variantes dans un seul fichier `model.yaml`
  (`model`, `base` avec ses `sources`, `metadataOverrides`, `config`, `customFields`), et tout son
  catalogue est écrit ainsi ([doc](https://www.lmstudio.ai/docs/app/modelyaml),
  [spec](https://modelyaml.org)).
- **llama-swap** déclare l'exclusivité une fois, par groupe (`swap: true`, `exclusive: true`,
  `members: [...]`), et non par paire de modèles
  ([config.example.yaml](https://raw.githubusercontent.com/mostlygeek/llama-swap/main/docs/config.example.yaml)).

Rien de cela ne demande Kubernetes ni une dépendance : la leçon est la séparation des données
(le modèle, ses variantes, ses mesures) et du mécanisme (l'installeur, la bascule, le cockpit).

### La première étape sûre, prototypée et testée ici

`docs/reviews/2026-10-21-prototypes/lanes.toml` décrit les huit lanes (id, unit, templates,
type, moteur, familles, cibles, drapeau `--with-`, fichier de staging, temps de démarrage par
défaut, ordre d'affichage). `tools/lanegen.py` le lit avec `tomllib` (stdlib depuis Python 3.11 ;
DGX OS 7 et le runner `ubuntu-24.04` ont Python 3.12) :

    python3 docs/reviews/2026-10-21-prototypes/tools/lanegen.py check

**[mesuré ici]** Sur `e07dd47`, sans rien modifier au dépôt : `8 lanes, 15 targets; 27 of 27
lists agree with it`. Les 27 listes vérifiées : les 18 lignes `Conflicts=`/`After=` des 9
templates, l'`After=` du proxy, le bloc sudoers (46 lignes exactes, sans joker : 5 verbes par lane et 6 installations de staging), cinq tables de
`base.js`, la garde de `switch-model.sh`, `FLAG_LANES` de `ci.yml`. Le manifeste dit donc
exactement ce que dit le dépôt : l'adopter ne change aucun comportement.

**[mesuré ici]** Sur `e07dd47`, `lanegen.py render` ne change pas un octet (0 template réécrit) :
il produit exactement les templates du dépôt. J'ai ensuite simulé une neuvième lane dans une copie :
- `render` réécrit d'un coup les 11 templates concernés (les `Conflicts=` et `After=` des 10
  templates de lanes, et l'`After=` du proxy), la modification en N² devenant une commande ;
- `check` passe de 0 à 21 listes d'accord sur 29, et nomme les 8 qui restent tenues à la main :
  le bloc sudoers, cinq tables de `base.js`, la garde de `switch-model.sh` et `FLAG_LANES`. Les
  autres listes du dépôt (64 en tout) ne sont pas lues par `check` ; `test_lane_lists_whole.py`
  continue de les garder jusqu'à leur conversion ;
- une lane retirée à la main d'un template est signalée, avec le fichier et les deux lignes.

### La migration, une liste à la fois (chaque étape est un commit vert)

1. **Phase 1, sans changement de comportement (1 à 2 jours).** Ajouter `lanes.toml` et
   `tools/lanegen.py` à la racine, et `lanegen.py check` comme porte de CI. Puis convertir les
   listes une par une, chaque commit gardant la CI verte :
   - les `Conflicts=`/`After=` rendus par les installeurs depuis le manifeste (ou réécrits par
     `render`, la porte vérifiant que le résultat est commité) ;
   - le bloc des lanes du sudoers généré, toujours en argv exact ;
   - les listes shell lues depuis `eval "$(python3 tools/lanegen.py shell)"` ou un
     `lanes.gen.sh` généré et vérifié ;
   - les tables de `base.js` servies par le cockpit (`/api/lanes`) ;
   - `test_lane_lists_whole.py` remplacé par `lanegen.py check` et par une porte qui interdit
     toute nouvelle liste écrite à la main.
2. **Phase 2 : les faits de chaque lane.** Les pins (dépôt, révision, digest, Go, part de
   mémoire, requêtes simultanées), le nom servi et les données du fournisseur opencode passent
   dans le manifeste ; `dashboard/registry.py`, `check-pins.sh` et `uninstall.sh --list` les
   lisent de là au lieu de les relire par regex dans neuf scripts. Les trois `oc-*.py` deviennent
   un générateur. Les blocs vLLM de
   `switch-model.sh` deviennent une fonction `switch_vllm_lane` dans `vllm-lane.sh`, qui existe
   déjà pour les installeurs depuis v1.35 (« a fix made in one was missing from the next »).
3. **Phase 3 : le catalogue.** Le cockpit affiche les lanes depuis le manifeste ; un modèle
   ajouté au manifeste apparaît dans l'interface. C'est le socle du « LM Studio du DGX Spark »,
   et le lieu naturel des profils matériels (GB10 aujourd'hui, d'autres GPU demain) sous forme
   de sections par machine plutôt que de branches de code.

**Gain attendu.** Pour une lane vLLM, le câblage de 35 fichiers partagés se réduit à une entrée
de manifeste. Il reste le contenu propre à la lane : son installeur ou ses pins, son template,
ses fixtures et ses tests, sa page de doc. Estimation fondée sur le découpage de FP4-Local
ci-dessus ; la vraie mesure sera la prochaine lane ajoutée après la phase 2.

**Risques.** La migration touche les scripts que les tests découpent par chaînes (section 5) :
sans la recommandation 3, chaque étape coûte des réécritures de tests. C'est, je pense, la
raison principale pour laquelle le registre proposé le 2026-10-18 n'a pas démarré.

## 5. Recommandation 3 : des tests qui vérifient ce que le code fait, pas ce qu'il dit

### Le problème, mesuré

La suite mesure sa couverture par module et son score de mutation, et contient des tests de
propriétés et du fuzzing. Mais une grande partie de ce qu'elle vérifie des scripts shell est leur
**texte** :

- **Au moins 125 des 232 fichiers de test** lisent le texte d'un script, d'un template ou de la
  page (`read_text()` sur un chemin en `.sh`, `.py`, `.template`, `.js`, `.html`, `.css` ou
  `.yml`) **[code]**.
- **89 fichiers** découpent ces textes sur des chaînes exactes, avec **au moins 489 ancres**
  `texte.index("...")` **[code]**. Le compte suit la provenance du texte : un appel compte quand
  son receveur a été lu d'un fichier du dépôt, ou découpé dans ce qui l'a été ; 66 autres appels,
  sur la sortie d'un programme ou une liste d'arguments, sont écartés. Vingt ancres tirées au
  hasard et relues à la main portaient toutes sur un source. Exemples : `tests/test_image_lane.py:220`
  vérifie qu'une ligne vient avant une autre dans `install-image.sh`
  (`assertLess(text.index(...), text.index(...))`) ; `tests/test_kolibri_pins_one_place.py`
  extrait le bloc Kolibri de `switch-model.sh` entre deux chaînes avant de l'exécuter.
- Dans `ci.yml`, 48 des 120 lignes `grep -q` portent sur le texte d'un script, d'un template ou du
  cockpit **[code]**. Une partie de ces gardes est légitime (un pin qui doit rester à sa place) ;
  le problème est leur nombre, pas leur existence.

L'intention est souvent comportementale (la tranche découpée est ensuite exécutée), mais le
couplage est textuel : un refactoring sans aucun changement de comportement casse ces tests.
**[mesuré ici]** Trois refactorings neutres, appliqués chacun à une copie du dépôt, puis les tests
qui lisent le fichier modifié :

| Refactoring sans changement de comportement | Fichiers de test qui lisent le script | Fichiers qui cassent | Tests qui cassent |
|---|---:|---:|---:|
| `install-image.sh` : renommer la fonction interne `restore_text_lane` | 27 | 1 (`tests/test_image_lane.py`) | 2 |
| `switch-model.sh` : renommer la variable interne `FLL_STAGE` (7 occurrences) | 47 | 0 (aucun) ; et l'étape de CI « Every privileged call of switch-model.sh is in the cockpit allowlist » s'arrête sur `KeyError: 'FLL_STAGE'` | 0 |
| `switch-model.sh` : écrire le test du bloc Flash FP4-Local en `[[ ]]` au lieu de `[ ]` | 47 | 2 (`tests/test_flash_local_stage.py`, `tests/test_lane_lists_whole.py`) | 2 |

Les trois changements sont neutres (les deux noms ne servent qu'à l'intérieur de leur script, et
`[[ $CHOICE == flash-local ]]` a le sens de `[ "$CHOICE" = "flash-local" ]` en bash), et chacun
casse quelque chose. Deux fichiers échouaient sur la copie de référence faute de `ss` dans le
conteneur (`test_fresh_machine.py`, `test_install_left_out_lane.py`) ; relancés une fois `ss`
installé, ils passent sur les quatre copies.

Ce que l'expérience montre, c'est où est le couplage : 45 des 47 fichiers qui lisent
`switch-model.sh` passent les trois changements ; ce qui casse, ce sont les lignes ancrées. La tête
d'un bloc (`test_flash_local_stage.py`), la forme `[ "$CHOICE" = "..." ]` que l'analyseur de
`test_lane_lists_whole.py` sait lire, un nom de variable que `ci.yml` relit par regex. Or le
refactoring que demande la phase 2 de la recommandation 2 (faire des blocs vLLM une fonction)
touche précisément ces lignes : les têtes de blocs de `switch-model.sh` servent d'ancres à
**8 fichiers de test** (`grep -lF 'if [ "$CHOICE" = "'`) **[code]**.

C'est la définition du test fragile chez Google : « a brittle test is one that fails in the face
of an unrelated change to production code that does not introduce any real bugs », alors que
« the ideal test is unchanging » et que, pour un refactoring pur, « the system's tests shouldn't
need to change » ([Software Engineering at Google, ch. 12](https://abseil.io/resources/swe-book/html/ch12.html)).

**Pourquoi c'est la recommandation qui débloque les autres.** Chaque refactoring de
`switch-model.sh` ou d'`install.sh`, et en particulier la migration vers le manifeste
(recommandation 2), coûte des dizaines de réécritures de tests, à 45 minutes de CI l'itération. Le
chemin le moins coûteux pour un agent devient alors de copier un bloc plutôt que de le factoriser,
d'où les blocs vLLM identiques à 73 à 77 %. L'outil de sécurité que sont les tests rend la
factorisation chère, et la duplication gagne.

**Un deuxième symptôme : les tests ont leurs propres listes de lanes.** `tests/test_text_lane.py`
vérifie la règle « la lane texte à laquelle la box revient » sur toutes les séquences de bascules
jusqu'à six, mais ses tables `UNIT` et `SIDES` énumèrent sept lanes : **Flash FP4-Local n'y est
pas** (`grep -c flash-local tests/test_text_lane.py` donne 0) **[code]**. La propriété n'est donc
pas vérifiée pour la huitième lane, et `test_lane_lists_whole.py` ne s'en aperçoit pas, parce
qu'il ne lit pas les tests.

### Ce que je propose

1. **Une porte à cliquet sur les ancres** (une heure) : `tools/anchor-ratchet.py`, fourni, compte
   les ancres de chaque fichier de test comme ci-dessus et les compare à `tests/anchor-ceiling.json`.
   Un fichier ne peut pas dépasser son plafond, un fichier absent du plafond n'en a droit à aucune,
   et `--write` refuse de relever un plafond. C'est la philosophie du dépôt pour la couverture
   (« floors only rise »), appliquée dans l'autre sens : on arrête d'en ajouter dès demain, sans
   rien réécrire. **[mesuré ici]** Sur `e07dd47` : 489 ancres dans 89 fichiers ; une ancre ajoutée
   dans un fichier existant, et une autre dans un nouveau fichier, font échouer la porte en nommant
   les deux fichiers ; une ancre retirée la laisse passer et propose d'abaisser le plafond.
2. **Des scripts organisés en bibliothèques de fonctions**, comme `lanes.sh` et `vllm-lane.sh`
   le sont déjà : le corps de chaque bloc de `switch-model.sh` devient une fonction appelée par un
   `main`, avec une garde `if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi` (pas la forme
   `[ ... ] && main "$@"`, qui renvoie 1 quand le fichier est sourcé et arrête un appelant sous
   `set -e` : la classe 3 du `repo-reviewer`). Un test fait
   `bash -c '. ./switch-model.sh; switch_vllm_lane kolibri'` derrière les bouchons de
   `tests/installer_wall.py` (`fence()`), au lieu de découper le fichier.
3. **Une règle de migration opportuniste** : quand un refactoring casse un test à ancre, on le
   réécrit contre une fonction ou contre un mur de `installer_wall.py`, pas en déplaçant l'ancre.
   Le compte du cliquet baisse à chaque fois.
4. **Les listes des tests viennent du manifeste.** `test_text_lane.py` tire ses lanes de
   `lanes.toml` : la huitième lane y entre d'elle-même, comme toutes les suivantes.

**Gain attendu.** Un refactoring sans changement de comportement ne casse plus que les tests qui
appellent une fonction renommée, c'est-à-dire des tests qu'on attend voir changer. La recommandation
2 devient une suite de petits commits verts au lieu d'une réécriture de tests.

**Risques.** Un test à ancre vérifie parfois un ordre qui compte vraiment (par exemple « la place
sur le disque est vérifiée avant le téléchargement »). Le réécrire en comportement (un mur après la
vérification, aucun téléchargement observé) est plus fort, mais demande de comprendre ce qu'il
protège : à faire au cas par cas, jamais en masse.

## 6. Recommandation 4 : ce qui arrive sur la box d'un utilisateur est exactement ce qui a été validé

La règle du dépôt est « Nothing unpinned reaches a user » (CONTRIBUTING.md). Elle est tenue pour
les checkpoints, les images et les wheels des lanes vLLM (digest, révision, sha256, `--no-deps`)
**[code]**. Cinq choses mesurables passent pourtant entre ses mailles.

### 6.1 `get.sh` installe un `main` que la CI n'a pas encore validé

- `get.sh` fait `git reset --hard origin/main` : son invariant écrit est « if this script
  completes, you ARE on the latest origin/main » **[code]**.
- Sur les 80 derniers runs de `ci.yml` déclenchés par un push sur `main` (du 2026-09-22 au
  2026-10-21) : durée médiane 42 minutes, maximum 81 **[historique, API GitHub Actions]**.
- **Sur les 8 derniers jours, `main` a été en avance sur sa CI entre 0,7 et 5,3 heures par jour,
  21,7 heures au total** ; le 2026-10-21, 5,0 heures. Pendant ces fenêtres, le one-liner installe
  un commit dont la CI n'a pas fini **[historique]**.
- Quatre de ces 80 runs ont échoué, et `main` est resté rouge, puis non validé, de 0,2 à 3,5
  heures avant le run vert suivant (le 2026-10-11 : 3,5 heures ; le 2026-10-15, sur le commit de
  release v1.33.0 : 2,3 heures). Le one-liner installait ce `main` **[historique]**.

**Proposition (prototypée, auditée ici).** Une branche `stable` qui n'avance que sur un commit de
release dont toute la CI est verte, et `get.sh` qui l'installe par défaut (`CHANNEL=main` pour
la pointe) :
- `docs/reviews/2026-10-21-prototypes/github/ci-promote-job.yml` est un job `promote` à ajouter
  à `ci.yml` (`needs: checks`, `contents: write` sur ce seul job, avance uniquement vers l'avant,
  et sans effet hors d'un runner GitHub, parce que `ci-local.sh` exécute toutes les étapes de tous
  les jobs). **[mesuré ici]** zizmor 1.31.1 ne trouve plus rien sur `ci.yml` ainsi modifié. Une
  première version en `workflow_run` était signalée « dangerous-triggers » : écartée.
- Côté `get.sh`, la règle à écrire : `fetch origin stable` et `reset --hard origin/stable`, sauf
  si la box est déjà en avance sur `stable` (une box qui suit `main` n'est jamais rétrogradée).
  La vérification de release du cockpit lit déjà les releases GitHub, donc la notion de « dernière
  version » ne change pas.
- La revue du 2026-10-18 posait la même question (sa proposition 7), sans les mesures.

### 6.2 Les lanes image et vidéo résolvent 232 paquets sur PyPI au moment de l'installation

`install-image.sh` et `install-video.sh` font `pip install "sglang[diffusion]==$WHEEL"`
(`WHEEL=0.5.21`) sans fichier de contraintes ni de hashes **[code]**. Le paquet de tête est épinglé ;
tout son arbre transitif est choisi le jour de l'installation. Le code le reconnaît (« The wheel
is also what sets the versions of everything the runtime imports »).

**[mesuré ici]** `uv pip compile` de cette ligne, pour la plateforme de la box
(`--python-platform aarch64-manylinux_2_39 --python-version 3.12`), donne aujourd'hui
**232 paquets**, dont torch 2.13.0, transformers 5.12.1, diffusers 0.37.0, flashinfer-python 0.6.18
et cache-dit 1.5.1, avec 4 989 hashes. Deux box installées à des dates différentes ne servent donc
pas le même runtime, et une version publiée demain sur PyPI entre chez l'utilisateur sans avoir été
mesurée.

**Proposition.** Un lock par lane, écrit depuis le venv mesuré de la box de référence
(`pip freeze`), hashé pour aarch64 / Python 3.12 (`uv pip compile --generate-hashes`, qui sait
compiler pour une autre plateforme), puis installé en mode hash-checking :
`pip install --require-hashes --only-binary :all: --no-deps -r image-lane.lock`. Avec ce mode, pip
refuse tout paquet non listé ou dont le hash diffère, et la doc recommande justement
`--only-binary :all:` en complément ([pip, secure installs](https://pip.pypa.io/en/stable/topics/secure-installs/)).
La source SGLang épinglée (`git clone` au commit, `--no-deps -e`) ne change pas. Un écart entre le
lock et `pip freeze` de la box de référence est **[à valider sur la box]** : c'est la première
mesure à faire.

### 6.3 Les actions GitHub sont épinglées par tag

**[mesuré ici]** zizmor 1.31.1 (`--offline .github/workflows/`) : 2 findings `unpinned-uses`
(niveau high, `actions/checkout@v5`) et 2 `artipacked` (niveau medium, `persist-credentials` non
désactivé). Le correctif est en deux lignes par workflow : `actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5.1.0`
(ce vers quoi `@v5` pointe aujourd'hui, vérifié par `git ls-remote`) et `persist-credentials: false`.
Après correctif, zizmor ne trouve plus rien **[mesuré ici]**. dependabot.yml surveille déjà
l'écosystème `github-actions` et sait mettre à jour un SHA épinglé. Ajouter zizmor comme étape
de CI ([docs](https://docs.zizmor.sh)) garde le résultat.

### 6.4 La box neuve est la première source de bugs des utilisateurs

- Sur les rapports de bugs ouverts par des utilisateurs externes, au moins 7 sur 13 portent sur
  l'installation ou le démarrage sur leur machine : #3 (locale allemande), #5, #14, #25, #48, #54,
  #57 **[historique, API GitHub]**.
- #25 (2026-09-30) : le one-liner échouait à l'étape 7 sur toute box neuve (la régression de
  v1.18.7). C'est un utilisateur qui l'a trouvée ; la correction est v1.19.0 **[historique]**.
- v1.37.2, v1.37.3 et v1.37.4 corrigent ce qu'une deuxième GX10 neuve a montré : compte hors du
  groupe docker, python3-dev et ffmpeg absents, mot de passe demandé par `sg`, et `ninja` absent
  alors que la box de référence l'avait reçu « by hand on 2026-02-11 », ce qui masquait le
  défaut **[code, CHANGELOG]**.

La box de référence n'est pas une box neuve, et les tests à bouchons (`tests/test_fresh_machine.py`,
60 cas) ne voient pas ce qu'un outil installé à la main cache. Deux mesures, de la moins chère à la
plus complète :
1. **Le rapport de dérive de la box de référence** (une heure de travail) : enregistrer une fois
   `dpkg-query -W -f '${Package}\n'` et la liste des commandes d'une DGX OS neuve (la deuxième
   GX10 vient d'en être une), la commiter comme fixture, et comparer avant chaque release mineure
   avec la box de référence. Chaque paquet en plus est un candidat « ninja » : un outil qu'un
   script ou un runtime utilise peut-être sans l'installer.
2. **La répétition d'une première installation dans un conteneur système jetable**, avec son
   propre systemd, son propre Docker et le GPU de la box. Canonical documente la recette (LXD,
   `security.nesting=true`, périphérique `gpu` en `gputype=physical`, NVIDIA Container Toolkit dans
   l'instance) ([LXD, GPU passthrough with Docker](https://canonical.com/lxd/docs/latest/howto/container_gpu_passthrough_with_docker/)) ;
   Incus, son fork, a les mêmes options. **[à valider sur la box]** Première étape : obtenir
   `docker run --gpus all ... nvidia-smi` dans une instance sur le GB10. Si ça marche, la
   répétition d'un `get.sh` complet (cache HF monté en lecture seule pour ne rien retélécharger)
   devient une étape de `/release` pour chaque version mineure.

### 6.5 Les conteneurs des moteurs tournent avec tous leurs droits

**[code]** Les 7 templates qui lancent un conteneur (`docker run`) utilisent tous
`--network host` ; aucun ne passe `--cap-drop` ni `--user`, et seul celui de Flash FP4-Local a un
`--security-opt` (son profil seccomp). Les deux lanes Nemotron passent `--trust-remote-code`
(`qwen38-nemotron.service.template:63`) : du Python venu du dépôt Hugging Face, à la révision
épinglée, exécuté en root dans un conteneur sur le réseau de l'hôte.

**Proposition.** Pour chaque lane, mesurer avec le protocole habituel l'effet de
`--cap-drop ALL --security-opt no-new-privileges` (règles 3 et 4 de l'[OWASP Docker Security
Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html)), et
ne garder que ce que la mesure accepte. Pour `trust-remote-code`, ajouter aux pins le sha256 des
fichiers `.py` du dépôt à la révision épinglée et refuser un écart, comme le dépôt le fait déjà
pour la release opencode. La sortie des moteurs du réseau de l'hôte est déjà au ROADMAP (« The
engines' coordination ports off the network »).

**Ce que le scanner GLM ne peut pas voir.** Un scanner qui lit le dépôt voit le code ; il ne voit
ni l'arbre de dépendances résolu le jour de l'installation (6.2), ni la fenêtre pendant laquelle
`main` n'est pas validé (6.1), et un outil dédié lit mieux que lui l'épinglage des workflows (6.3).
Ces contrôles sont déterministes et se rejouent à chaque CI : ils complètent le scanner, ils ne le
remplacent pas.

## 7. Recommandation 5 : ton savoir-faire dans le dépôt, et les règles dures dans des hooks

### Le problème, mesuré

- **Le dépôt ne versionne rien de ton outillage** : ni `CLAUDE.md`, ni `.claude/` **[code]**.
  Tout vit dans `~/.claude/` sur la box. La doc de Claude Code est explicite : « Auto memory is
  machine-local », et « Files are not shared across machines or cloud environments »
  ([memory](https://code.claude.com/docs/en/memory)). Conséquence directe : cette session,
  comme celle du 2026-10-18, a travaillé sans aucune de tes leçons. Une panne du disque de la box
  les emporte toutes.
- **La mémoire n'applique rien.** « Claude treats them as context, not enforced
  configuration » : pour qu'une règle tienne quoi que décide le modèle, la doc renvoie aux hooks ([memory](https://code.claude.com/docs/en/memory),
  [hooks](https://code.claude.com/docs/en/hooks)). Les règles dures du dépôt (pas de tiret
  cadratin, stdlib seulement, pas de secret en argv, CI complète avant un push sur `main`) ne sont
  aujourd'hui vérifiées que par la CI, donc au mieux 45 minutes après l'erreur.
- **La review arrive souvent après le tag.** « found in review » figure sur 645 lignes des
  fichiers suivis, et 240 de ces annotations sont datées du 2026-10-14 au 2026-10-20 **[historique]**. Les
  reviews trouvent beaucoup ; quand elles passent avant la release (v1.37.5, v1.37.7, où deux
  correctifs ont été « stopped by their review, before any release »), c'est gagné. Quand elles
  passent après, c'est une release de plus : deux des sept correctifs de v1.37.x (v1.37.2 et
  v1.37.3) livrent ce qu'une review a trouvé après la release précédente, et huit tags sont sortis
  en 36 heures (v1.37.0 le 2026-10-20 à 12:16, v1.37.7 le 2026-10-22 à 00:35, heure de Paris)
  **[historique]**.
- **La méthode pour ajouter un modèle est implicite.** CONTRIBUTING.md le dit lui-même : « The
  fastest way to learn the full list is to add the model and read what CI fails on ». Chaque
  élément oublié coûte un cycle de CI **[code]**.
- **Les propositions se perdent.** Celles du 2026-10-18 vivaient dans une PR fermée et n'ont
  jamais atteint ROADMAP.md, qui est pourtant l'endroit où le dépôt dit que « on the roadmap
  means a checkbox with a definition of done » **[code]**.
- **Le contexte que tu relis à chaque tâche est lourd.** `keepalive-proxy.py` fait 5 012 lignes,
  dont 67 % de commentaires et docstrings (3 071 contre 1 540 de code) ; `dashboard/cockpit.py`
  5 639 lignes, dont 54 % **[mesuré ici]**. Une grande part de ces commentaires raconte l'histoire
  (« found in review, 2026-10-20 »), que le CHANGELOG (7 512 lignes) et git racontent déjà.
  Anthropic et Chroma ont mesuré que la fiabilité d'un modèle baisse quand le contexte grandit
  (« context rot ») ; la consigne est de viser « the smallest set of high-signal tokens »
  ([Anthropic, 2025-09-29](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents),
  [Chroma, 2025-07-14](https://research.trychroma.com/context-rot)).

### Ce que je propose, prêt à l'emploi

Tous les fichiers sont dans `docs/reviews/2026-10-21-prototypes/claude/`, sous des noms que
Claude Code ne charge pas d'office (un fichier nommé `CLAUDE.md` dans un sous-dossier du dépôt
serait chargé dès que tu lis un fichier voisin). Tu décides de leur place après la phase 0.

1. **`CLAUDE.project.md`, à placer en `CLAUDE.md` à la racine** (58 lignes, la doc
   recommande de rester sous 200). Ce sont des consignes, pas de l'histoire : les « jamais »
   (sudo, fraction mémoire, tirets, dépendances, secrets, `/tmp`), les « toujours » (mesuré ou
   rien, un test nomme son bug, tester le comportement, une seule liste des lanes, `|| true` après
   un `grep` sous `pipefail`), l'échelle de vérification (section 3), et où sont les choses.
2. **Deux hooks, testés ici** (`hooks/fast-checks.sh`, `hooks/guard-release.sh`, câblés par
   `settings.proposed.json`, à copier dans `.claude/settings.json`) :
   - `PostToolUse` sur `Edit|Write` : les portes statiques de la CI sur le seul fichier écrit
     (`bash -n`, shellcheck, ruff F/E9, `node --check`, tirets, placeholders, blocs de code).
     **[mesuré ici]** 50 à 120 ms par fichier ; il attrape une erreur de syntaxe shell, un nom
     Python indéfini, un tiret cadratin, un placeholder et un bloc de code non fermé. Son code de
     sortie 2 renvoie la raison à Claude, qui corrige dans le même tour
     ([hooks](https://code.claude.com/docs/en/hooks)) ;
   - `PreToolUse` sur `Bash` : refuse la création d'un tag, un `gh release` qui publie et un
     `git push` vers `main` ou de tags tant que `.cache/ci-pass` ne contient pas le hash de
     l'arbre courant. `ci-local.sh` l'écrirait après une exécution complète finissant par
     « 0 echecs, 0 sautes », sur un arbre propre (patch d'une quinzaine de lignes, testé, en
     annexe C). Le hook découpe la ligne comme le shell (`shlex`) : `git tag -l`,
     `echo "git push origin main"` ou le push d'une branche `feature/maintenance` passent.
     **[mesuré ici]** 24 commandes, toutes classées comme attendu : bloquées sans tampon ou sur
     un arbre modifié, autorisées avec le tampon, et le push d'une branche de travail reste libre.
     C'est la règle que CONTRIBUTING.md pose déjà (« the whole thing before pushing »), rendue
     déterministe.
3. **Trois skills** (`skills/new-lane`, `skills/qualify-lane`, `skills/release`), au format
   officiel ([skills](https://code.claude.com/docs/en/skills)) :
   - `/new-lane` : les faits à réunir avant d'écrire une ligne (révision sur 40 caractères hexa,
     digest, licence, architecture enregistrée dans l'image, `trust-remote-code`), le scaffold
     depuis le plus petit installeur vLLM, `lanegen render && lanegen check`, des fixtures réelles,
     puis la mesure ;
   - `/qualify-lane` : le protocole de CONTRIBUTING.md (« discard the first batch, repeat three
     times, compare medians ») exécuté par un script qui attend les démarrages de 5 à 17 minutes à
     ta place et écrit un rapport JSON unique, d'où viennent tous les chiffres des docs. Le
     script est à écrire, la skill donne sa spécification exacte, avec les vrais instruments du
     dépôt (`bench.sh`, `bench-matrix.sh`, `tools-check.py --min`, `needle.sh`, `conc-check.py`,
     `bench-agent.py`, `evals/`) ;
   - `/release` (`disable-model-invocation: true`, puisqu'elle a des effets de bord) : la review
     sur tout le diff depuis le dernier tag **avant** le tag, le CHANGELOG, la CI complète sur ce
     commit, puis le tag.
4. **Un sous-agent `repo-reviewer`** (`agents/repo-reviewer.md`, sans Edit ni Write) qui
   cherche les dix classes de défauts que ce dépôt a réellement livrées, tirées du CHANGELOG et de
   TESTING.md : la box qui n'est pas la box de référence, la liste de lanes incomplète,
   `set -e`/`pipefail`, la valeur calculée avant la convergence, l'argv sudo, systemd, les
   secrets, le test qui passe pour une mauvaise raison, la doc qui ment, la page sous le doigt. Il
   rapporte « nothing found » pour chaque classe vérifiée, pour que le silence veuille dire
   vérifié et non oublié. À utiliser à la place, ou en plus, de la skill code-review générique.
5. **Les propositions acceptées vont dans ROADMAP.md**, avec leur définition de fini, au moment
   où elles sont acceptées. C'est ce qui a manqué le 2026-10-18.
6. **Un plafond mémoire pour tes propres commandes.** Tu lances tes tests et tes constructions sur
   la box qui sert, où une mémoire poussée à zéro la gèle jusqu'à une coupure de courant (README,
   « The GB10 unified-memory trap »). Claude Code sait plafonner, par un cgroup, la mémoire de
   toutes les commandes Bash et Monitor d'une session : `CLAUDE_CODE_TOOL_MEMORY_LIMIT=24G` par
   exemple ([tools reference](https://code.claude.com/docs/en/tools-reference)). Le plafond ne
   touche pas les moteurs (systemd et Docker les placent dans leurs propres cgroups) ; il protège
   la box d'un test ou d'un `pip install` qui s'emballe. **[à valider sur la box]** La valeur se
   calibre sur ta commande la plus lourde (la construction du venv d'une lane image) ; un
   dépassement tue la commande sans que son résultat nomme le plafond, à garder en tête.
7. **Les commentaires disent le présent.** Le pourquoi du code tel qu'il est maintenant reste en
   commentaire ; l'histoire de comment il en est arrivé là va dans le message de commit et le
   CHANGELOG, où elle est déjà. À appliquer au fil des modifications, pas en une passe : le but
   est de cesser d'en ajouter.

**Gain attendu.** Les erreurs statiques sont corrigées dans le tour où elles sont écrites au lieu
d'arriver 45 minutes plus tard ; la CI complète n'est plus oubliée ni lancée trop tôt ; une session
cloud, un autre poste ou une réinstallation de la box repart avec les mêmes règles ; la review
passe avant le tag.

**Risques.** Un hook lent ou bavard gêne plus qu'il n'aide : celui-ci reste sous 200 ms et ne parle
qu'en cas de problème. Un CLAUDE.md qui grossit perd en adhérence (la doc le dit) : garde-le sous
200 lignes et déplace le détail dans `.claude/rules/` à portée de chemins.

## 8. Ce que j'ai examiné et écarté, et pourquoi

- **Passer à pytest et pytest-xdist.** Gain réel en parallélisme, mais le dépôt a fait du comptage
  « déclaré contre exécuté » et de l'exécution autonome de chaque fichier des règles de CI, et
  garde la stdlib comme seule dépendance du runtime. `affected-tests.py` donne l'essentiel du gain
  sans changer de runner.
- **Réécrire `install.sh` en Python.** Trop de risque pour le gain, et les tests à ancres
  rendraient la bascule invérifiable. Le manifeste et les bibliothèques de fonctions apportent les
  gains par petites étapes vertes.
- **Adopter OME, KServe ou Kubernetes.** Pensés pour un parc de machines, démesurés pour une box.
  On reprend leur idée (séparer le modèle du runtime), pas leur plateforme.
- **Remplacer la bascule de lanes par llama-swap.** Il ne connaît ni la garde mémoire du GB10
  (`engine-preflight.sh`), ni les preuves par génération réelle, ni l'intégration systemd et
  sudoers. On reprend l'idée du groupe exclusif déclaré une fois.
- **Déclarer `Conflicts=` d'un seul côté.** systemd crée la relation inverse (`ConflictedBy=`) et
  ordonne l'arrêt avant le démarrage dès qu'une dépendance d'ordre existe dans un sens
  ([systemd.unit](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html)),
  donc une nouvelle lane pourrait ne modifier que sa propre unit. Mais la règle actuelle (les deux
  sens) a été mesurée sur la box avec des units jetables, et le manifeste la génère sans effort :
  je ne propose pas d'y toucher sans une mesure identique.
- **Raccourcir le CHANGELOG.** C'est la mémoire du projet, et une mémoire versionnée. Ce qui pèse
  sur ton contexte, c'est sa duplication dans les commentaires du code (section 7), pas son
  existence.
- **Ajouter des fonctionnalités au cockpit.** Hors sujet ici : chaque recommandation porte sur la
  vitesse et la sûreté du développement, pas sur le produit.

## 9. Plan d'exécution proposé

Chaque ligne est un ou plusieurs commits verts, avec sa définition de fini. L'ordre suit le
rapport gain sur effort, et la dépendance entre les recommandations.

| Quand | Quoi | Fini quand |
|---|---|---|
| Jour 1 | Phase 0 (section 2) | l'inventaire est fait, et chaque élément a sa case |
| Jour 1 | Hooks `fast-checks` et `guard-release`, tampon de `ci-local.sh`, `.cache/` dans `.gitignore` | un tiret écrit est signalé dans le tour ; un tag sans CI complète est refusé |
| Jour 1 | Actions épinglées par SHA, `persist-credentials: false`, zizmor en CI (6.3) | zizmor ne trouve rien |
| Jour 1 | `patches/proxy-drop-test-race.diff` et `patches/cockpit-env-leak.diff` (section 3) | le test du proxy passe sous charge ; la sonde du cockpit voit chaque classe |
| Jours 2 et 3 | `patches/test-text-lane-reduced.diff` et `patches/env-probe-instead-of-rerun.diff` (correctifs 1 et 2 de la section 3), puis `affected-tests.py` adopté et sa carte construite sur la box | les deux étapes lourdes perdent ce que la section 3 mesure ; un commit courant se valide en quelques minutes |
| Jour 2 | `tools/anchor-ratchet.py` en CI, son plafond écrit une fois (section 5) | aucune ancre nouvelle n'entre |
| Jour 3 | `CLAUDE.md` de projet, skills `/release` et `/new-lane`, sous-agent `repo-reviewer` (section 7) ; les propositions acceptées entrent dans ROADMAP.md | la prochaine release passe par `/release` |
| Semaine 2 | Manifeste, phase 1 (section 4) | `lanegen.py check` est une porte de CI ; plus aucune liste de lanes nouvelle écrite à la main |
| Semaine 2 | Canal `stable` (6.1) | `get.sh` installe `stable` par défaut ; `stable` n'avance que sur une release verte |
| Semaine 3 | Locks hashés des venvs image et vidéo (6.2) ; rapport de dérive de la box de référence (6.4) | une installation neuve reproduit le venv mesuré, octet pour octet |
| Ensuite | Manifeste phases 2 et 3 ; script `qualify-lane` ; répétition d'installation en conteneur système ; durcissement mesuré des conteneurs (6.5) | la prochaine lane ne touche que ses propres fichiers et le manifeste |

## Annexe A. La CI complète, chronométrée étape par étape

**[mesuré ici]** Une copie de la boucle de `ci-local.sh` qui chronomètre chaque étape, sur
`e07dd47`, dans le conteneur décrit en section 0 (x86_64, 4 vCPU), avec les paquets épinglés par
la CI. Les durées absolues ne sont pas celles de la box ; leur répartition est ce qui sert. Une
partie du temps, une expérience de la section 5 tournait à côté en priorité minimale (`nice 19`) :
elle ne prenait que le processeur laissé libre.

| Étape de `ci.yml` | Durée ici | Part du total | Statut |
|---|---:|---:|---|
| No test file in tests/ is left unrun | 2404 s | 48 % | échec (root, voir plus haut) |
| The offline suite touches nothing outside itself | 1195 s | 24 % | échec (root, voir plus haut) |
| Coverage floors, the root modules | 320 s | 6 % | ok |
| Coverage floors (measured, per module) | 305 s | 6 % | ok |
| Mutation score (do the tests catch a wrong operator?) | 261 s | 5 % | ok |
| Cockpit unit tests (every tests/test_*.py, discovered) | 259 s | 5 % | ok |
| The cockpit page works under a finger (headless browser, real taps, a cockpit of its own) | 108 s | 2 % | ok |
| Proxy fuzz and relay simulation (hypothesis, generated bytes and clients) | 46 s | 1 % | ok |
| shellcheck (warning severity) | 38 s | 1 % | ok |
| The cockpit survives a click storm on every view (headless browser, a cockpit of its own) | 38 s | 1 % | ok |
| Property-based checks (hypothesis, generated inputs) | 15 s | 0 % | ok |
| The cockpit page survives losing its server (headless browser, a cockpit of its own) | 11 s | 0 % | ok |
| bench.sh names the lane it measures (fake engine) | 5 s | 0 % | ok |
| les 36 autres étapes (greps, rendus, gardes) | 16 s | 0 % | 35 ok |
| **total** | **84 min** | 100 % | 46 ok, 3 échecs, 0 sautées |

Les trois étapes en échec le sont parce que le conteneur exécutait tout en root : `get.sh` refuse
root (c'est son premier mur), et les tests qui échouent vérifient justement ce qu'un utilisateur
ordinaire ne peut pas faire (`test_uninstall_reclaim.py` : un répertoire qu'il ne peut pas vider
demande `sudo` ; `test_ple_table.py` : un dossier qu'il ne peut pas créer ni écrire ;
`test_proxy_client_keys.py` : un fichier de clés illisible), ou passent par les murs d'installeurs
qui refusent root. Relancées en utilisateur ordinaire sur le même commit, avec ce qui manquait à ce conteneur (`ss`, du paquet iproute2, que DGX OS et les runners GitHub ont ; `jinja2` pour l'interpréteur système, que l'étape installe sur un runner), elles tiennent : `get.sh guards` passe ; l'étape hors ligne passe (26 minutes), au deuxième essai, le premier ayant échoué sur le test instable décrit en section 3 ; l'exécution commune de `tests/` exécute ses 2 037 tests sans un échec (« OK »), et son compte « déclarés contre exécutés » ne différait que des 4 tests de `test_lean_level.py`, qui sautent sans `jinja2`. Sur la box comme sur un runner GitHub, la CI ne tourne pas en root.

## Annexe B. Comment chaque chiffre a été obtenu

Toutes les commandes partent de la racine du clone, sur `e07dd47`, avec l'historique complet
(`git fetch --unshallow`).

**Historique et coût des lanes**
- Commits et tags : `git log --oneline | wc -l` (898), `git tag | wc -l` (121).
- Coût d'une lane : `git rev-list --count A..B`, `git diff --stat A..B` et `git diff --numstat A..B`
  sur `v1.28.2..v1.29.0` (Kolibri), `v1.33.1..v1.34.0` (Bolt), `v1.34.0..v1.35.0` (Super VL),
  `v1.36.4..v1.37.0` (FP4-Local). Le découpage « fichiers propres / fichiers partagés » de FP4-Local
  range dans les premiers `install-flash-local.sh`, `qwen38-flash-local.service.template`,
  `ple-table.py`, `seccomp-io-uring.json` et `oc-flash-local.py`, hors `tests/`, `docs/` et `*.md`.
- Releases v1.37.x : `git tag -l 'v1.37.*' --sort=creatordate --format='%(creatordate:iso8601) %(refname:short)'`,
  et le texte de chaque entrée du CHANGELOG.
- « found in review » : `git grep -c 'found in review'`, puis par date avec
  `git grep -h -o -E '\(found in review, 20[0-9-]+\)' | sort | uniq -c`.

**Listes de lanes et duplication**
- Les 64 listes : les fonctions `shell_files`, `js_literals`, `assigned_names`, `checked` et les
  familles de `tests/test_lane_lists_whole.py`, appliquées aux scripts racine, aux templates, au
  Python de `dashboard/` et au JavaScript de la page ; une liste compte dès qu'elle nomme trois lanes.
- Les blocs vLLM de `switch-model.sh` (lignes 117 à 269, 270 à 475, 476 à 610 sur `e07dd47`) :
  commentaires retirés, noms de lanes remplacés par un jeton commun, puis
  `difflib.SequenceMatcher(autojunk=False)` sur les lignes ; part des lignes identiques rapportée
  au bloc le plus court.
- Ancres : `tools/anchor-ratchet.py` (sa docstring dit la règle) ; les 20 cas relus à la main ont été tirés
  avec `random.seed(7)`. Fichiers qui lisent un source : `read_text(` appliqué à un chemin littéral en
  `.sh`, `.py`, `.template`, `.js`, `.html`, `.css` ou `.yml` (`grep -lE`), d'où « au moins ».
- `grep -q` de `ci.yml` : `grep -nE "grep -q" .github/workflows/ci.yml | grep -cE
  "install[a-z-]*\.sh|switch-model\.sh|\.template|cockpit|lanes\.sh|vllm-lane\.sh|uninstall\.sh|get\.sh|base\.js|index\.html"`.
- Commentaires : lignes non vides commençant par `#`, ou à l'intérieur d'une docstring, rapportées
  aux lignes non vides.

**CI**
- Durées sur GitHub : API Actions, `list_workflow_runs` de `ci.yml`, filtrés `event=push` et
  `branch=main`, `updated_at - run_started_at`. Fenêtre « `main` en avance sur sa CI » : union par
  jour des intervalles entre le début d'un run et sa fin.
- Durées par étape ici : une copie de la boucle de `ci-local.sh` qui chronomètre chaque étape
  (même HOME et même TMPDIR jetables, mêmes bouchons pour les outils absents), en root, avec
  `coverage==7.16.0`, `hypothesis==6.168.0`, `jinja2==3.1.6`, `ruff==0.5.0`, shellcheck 0.9.0,
  Node 22 et le Chromium de Playwright (`CHROME=`).

**Sécurité et dépendances**
- zizmor : `pip install zizmor` (1.31.1), `zizmor --offline .github/workflows/`.
- Le SHA de `actions/checkout` : `git ls-remote https://github.com/actions/checkout 'refs/tags/v5*'`.
- L'arbre du venv image : `uv pip compile` sur `sglang[diffusion]==0.5.21`, `--generate-hashes
  --python-platform aarch64-manylinux_2_39 --python-version 3.12 --prerelease allow` (uv 0.13.4).
- Les drapeaux des conteneurs : `grep` de `--network host`, `--cap-drop`, `--user`,
  `--security-opt`, `trust-remote-code` dans chaque template contenant `docker run`.

**Issues**
- API GitHub, `list_issues` (20 issues), lecture de #25 et #48.

## Annexe C. Les prototypes fournis, et comment les adopter

Tout est dans `docs/reviews/2026-10-21-prototypes/`. Rien n'y est chargé ni exécuté par le dépôt
ou par Claude Code tant que tu ne le déplaces pas. Chaque fichier passe ici les portes que la CI
applique à tout fichier suivi (tirets, placeholders, blocs de code, `bash -n`, shellcheck
`-S warning`, ruff F/E9).

| Fichier | Rôle | Testé ici | Pour l'adopter |
|---|---|---|---|
| `lanes.toml` | le manifeste des huit lanes (recommandation 2) | `check` : 27 listes sur 27 d'accord | `git mv` à la racine |
| `tools/lanegen.py` | `check`, `render`, `shell`, `json` | dérive détectée, neuvième lane simulée | `git mv` dans `tools/`, puis une étape de CI `python3 tools/lanegen.py check` |
| `tools/anchor-ratchet.py` | le cliquet des ancres (recommandation 3) | plafond écrit, deux fichiers qui grossissent refusés, un qui baisse accepté, plafond jamais relevé | `git mv` dans `tools/`, `--write` une fois, puis une étape de CI `python3 tools/anchor-ratchet.py` |
| `tools/affected-tests.py` | carte test vers fichiers lus (strace), sélection et exécution (recommandation 1) | voir section 3 | `git mv` dans `tools/` ; `.cache/` dans `.gitignore` ; `sudo apt install strace` pour `map` seulement |
| `claude/CLAUDE.project.md` | consignes du projet, 58 lignes | portes statiques | à fusionner avec ton CLAUDE.md après la phase 0, puis à placer en `CLAUDE.md` à la racine |
| `claude/hooks/fast-checks.sh` | hook `PostToolUse` sur `Edit\|Write` | 8 fichiers (6 défauts, 2 propres), 50 à 120 ms | `.claude/hooks/`, câblé par `settings.proposed.json` |
| `claude/hooks/guard-release.sh` | hook `PreToolUse` sur `Bash` | 24 commandes dans un dépôt jetable (lecture de tags, push de branche de travail, push vers `main`, tags, `gh release`, tampon présent ou non) | `.claude/hooks/` ; demande le tampon de `ci-local.sh` |
| `claude/settings.proposed.json` | le câblage des deux hooks | JSON valide | à fusionner dans `.claude/settings.json` |
| `patches/test-text-lane-reduced.diff` | `test_text_lane.py` sur les huit lanes : exhaustif jusqu'à quatre bascules, puis 300 séquences tirées avec une graine fixe, et le test ciblé qui manquait (correctif 1 de la section 3) | ses 22 tests passent, dont un nouveau qui tue deux mutants que le fichier actuel laisse passer (section 3) | `git apply` ; puis remettre à jour la ligne `first=` de l'étape hors ligne (ses fichiers les plus longs) |
| `patches/env-probe-instead-of-rerun.diff` | la sonde `sitecustomize` de l'étape hors ligne compare l'environnement au début et à la fin de chaque fichier ; `test_suite_isolation.py` perd sa relance complète (correctif 2) | un fichier qui laisse `UPSTREAM` changé est nommé, un fichier propre ne l'est pas ; la CI « après » de la section 3 | `git apply` ; les docstrings qui disent « tests/test_suite_isolation.py holds it » nomment ensuite la sonde |
| `patches/cockpit-env-leak.diff` | `test_cockpit_logs_masked.py` rend l'environnement qu'il a trouvé ; `test_env_isolation.py` lance le premier test de chaque classe, pas seulement de chaque module (section 3, correctif 2) | la sonde corrigée échoue sur la fuite sans le correctif et passe avec | `git apply` |
| `patches/proxy-drop-test-race.diff` | `test_proxy_engine_withholds_headers.py` attend la ligne de fin du proxy au lieu de la lire une fois (section 3) | 1 échec sur 30 sans, 0 sur 30 avec, en alternance sous charge | `git apply` |
| `patches/ci-local-stamp.diff` | `ci-local.sh` écrit `.cache/ci-pass` après une exécution complète, propre, sans saut | exécutions propre et modifiée sur un dépôt jouet | `git apply` |
| `claude/skills/new-lane/SKILL.md`, `qualify-lane/`, `release/` | les trois procédures (recommandation 5) | front-matter conforme à la doc | `.claude/skills/` |
| `claude/agents/repo-reviewer.md` | le relecteur des dix classes de défauts | front-matter conforme à la doc | `.claude/agents/` |
| `claude/inventory.sh` | l'inventaire de la phase 0, en lecture seule | exécuté dans ce conteneur | à lancer tel quel |
| `github/ci-promote-job.yml` | le job `promote` du canal `stable` (recommandation 4) | zizmor : 0 finding sur `ci.yml` modifié | à ajouter sous `jobs:` de `ci.yml` |

Deux précautions :
- `tools/affected-tests.py` et `tools/lanegen.py` trouvent la racine du dépôt par git, donc
  tournent aussi depuis ce dossier, avant tout déplacement.
- Le job `promote` ne fait rien hors d'un runner GitHub (`GITHUB_ACTIONS=true`), parce que
  `ci-local.sh` exécute toutes les étapes de tous les jobs : sans cette garde, une CI locale
  pousserait la branche `stable` depuis la box.

## Annexe D. Sources

Claude Code (documentation officielle, lue le 2026-10-21) :
- Mémoire, CLAUDE.md, `.claude/rules/`, auto-mémoire (limites de 200 lignes ou 25 Ko, stockage
  local à la machine) : https://code.claude.com/docs/en/memory
- Skills (emplacements, front-matter, moins de 500 lignes, `disable-model-invocation`) :
  https://code.claude.com/docs/en/skills
- Hooks, référence (événements, code de sortie 2, `CLAUDE_PROJECT_DIR`, `async`) :
  https://code.claude.com/docs/en/hooks
- Hooks, guide (exemples `PostToolUse` sur `Edit|Write`, emplacement des réglages) :
  https://code.claude.com/docs/en/hooks-guide
- Sous-agents (`.claude/agents/`, front-matter, moindre privilège, `isolation: worktree`) :
  https://code.claude.com/docs/en/sub-agents
- Outils, dont le plafond mémoire des commandes (`CLAUDE_CODE_TOOL_MEMORY_LIMIT`, un cgroup) :
  https://code.claude.com/docs/en/tools-reference

Tests et CI :
- Software Engineering at Google, chapitre 12, Unit Testing (tests fragiles, « the ideal test is
  unchanging ») : https://abseil.io/resources/swe-book/html/ch12.html
- Predictive Test Selection, Facebook, 2018 (limites de la sélection par graphe de dépendances,
  efficacité doublée) : https://engineering.fb.com/developer-tools/predictive-test-selection/ et
  ICSE-SEIP 2019 : https://arxiv.org/abs/1810.05286
- coverage.py, contextes dynamiques (« quel test a exécuté cette ligne ») :
  https://coverage.readthedocs.io/en/latest/contexts.html
- Runners hébergés par GitHub (4 vCPU et 16 Go gratuits et illimités sur dépôt public, runners
  arm64 `ubuntu-24.04-arm`) : https://docs.github.com/en/actions/reference/runners/github-hosted-runners

Service de modèles :
- OME, séparation modèle et runtime : https://lmsys.org/blog/2025-07-08-ome et
  https://docs.sglang.io/ome/docs/concepts/serving_runtime
- LM Studio `model.yaml` : https://www.lmstudio.ai/docs/app/modelyaml et https://modelyaml.org
- llama-swap, groupes exclusifs :
  https://raw.githubusercontent.com/mostlygeek/llama-swap/main/docs/config.example.yaml
- systemd.unit, `Conflicts=`, `ConflictedBy=`, ordre arrêt avant démarrage :
  https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html

Chaîne d'approvisionnement et durcissement :
- pip, installations sûres et mode hash-checking : https://pip.pypa.io/en/stable/topics/secure-installs/
- uv, `pip compile --generate-hashes` et `--python-platform` (dont `aarch64-manylinux_2_39`) :
  https://docs.astral.sh/uv/reference/cli/
- zizmor, analyse statique des workflows GitHub Actions : https://docs.zizmor.sh
- OWASP Docker Security Cheat Sheet (règles 2, 3, 4 et 8) :
  https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html
- LXD, passthrough GPU vers Docker dans un conteneur système :
  https://canonical.com/lxd/docs/latest/howto/container_gpu_passthrough_with_docker/

Contexte des agents :
- Anthropic, Effective context engineering for AI agents (2025-09-29) :
  https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Chroma, Context Rot (2025-07-14) : https://research.trychroma.com/context-rot

Ce dépôt :
- La revue cloud du 2026-10-18 : https://github.com/hasso5703/dgx-spark-qwen38/pull/52
- Issues citées : https://github.com/hasso5703/dgx-spark-qwen38/issues/25,
  https://github.com/hasso5703/dgx-spark-qwen38/issues/48,
  https://github.com/hasso5703/dgx-spark-qwen38/issues/57
