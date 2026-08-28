# flow2obsidian

Un cours dicté dans **Wispr Flow** devient une fiche de révision structurée dans
**Obsidian**, sans intervention. Entre les deux, **Claude Code** fait le travail
de mise en forme.

```
Le prof arrive
   │  triple-tap sur la touche de dictée Wispr → le Notetaker enregistre
   ▼
Wispr Flow  →  flow.sqlite  (séance, notes, résumé)
   │  agent launchd : réveillé dès que la base bouge, + filet toutes les 10 min
   ▼
flow2obsidian  →  claude -p  (headless, sans outils)
   │  → JSON structuré : matière, plan, notions, questions d'examen, devoirs
   ▼
Vault Obsidian  (fiche + transcript + index matière + note du jour)
```

## Ce qui est produit

| Dossier | Contenu |
| --- | --- |
| `10 Cours/<Matière>/` | la fiche : résumé, déroulé, notions clés, à retenir, à retravailler, questions d'examen probables, devoirs en cases à cocher |
| `20 Transcripts/<Matière>/` | le verbatim, lié à la fiche |
| `30 Matieres/<Matière>.md` | index régénéré à chaque séance |
| `40 Journal/<date>.md` | note du jour avec le lien vers le cours |
| `90 Meta/captures-brutes/` | le matériau brut en JSON, pour régénérer autrement |

Dans les index de matière, tout ce qui est écrit **hors** des marqueurs
`<!-- flow2obsidian:debut -->` / `<!-- flow2obsidian:fin -->` est préservé d'une
régénération à l'autre.

## Installation

Prérequis : Python 3, [Claude Code](https://claude.com/claude-code) authentifié,
Obsidian, Wispr Flow.

```bash
git clone https://github.com/messyhat555/flow2obsidian.git
cd flow2obsidian
./install.sh
```

L'installeur détecte le vault Obsidian et la base Wispr Flow, crée
l'arborescence, installe la commande `flow2obsidian` et charge l'agent launchd.
Il se termine par un diagnostic.

Pour vérifier toute la chaîne sur un cours fictif :

```bash
flow2obsidian selftest
```

## Utilisation

Enregistre la séance avec le Notetaker de Wispr Flow — un triple-tap sur la
touche de dictée. Arrête à la fin du cours. La fiche apparaît dans les minutes
qui suivent.

```bash
flow2obsidian doctor     # vérifie l'installation
flow2obsidian status     # ce qui est traité, ce qui a été ignoré
flow2obsidian run        # force un passage immédiat
flow2obsidian backfill   # rattrape les anciennes séances
flow2obsidian redo <id>  # ré-exporte une séance
flow2obsidian import cours.txt --matiere "Droit constitutionnel"
```

`import` accepte `.txt`, `.md`, `.vtt`, `.srt` : n'importe quel transcript peut
entrer dans la chaîne, quelle qu'en soit l'origine.

## Nommer le cours

Quatre moyens, du plus fort au plus faible — le premier qui répond gagne.

**1. En ligne de commande**

```bash
flow2obsidian import cours.txt --matiere "Droit constitutionnel" --titre "La hiérarchie des normes"
```

**2. Dans le titre de l'enregistrement Wispr** — `Matière — Titre`
(séparateurs : `—`, `–`, ` : `, ` | `, ` / `, ` - `). La partie gauche n'est
retenue que si elle correspond à une matière connue ou à un alias, pour ne pas
découper un titre ordinaire par accident.

> `Droit constit — Le Conseil d'État` ➜ matière **Droit constitutionnel**,
> titre **Le Conseil d'État**

**3. Par l'emploi du temps** — rien à taper, la matière se déduit du jour et de
l'heure :

```json
"emploi_du_temps": [
  {"jours": ["lundi", "jeudi"], "debut": "08:00", "fin": "10:00",
   "matiere": "Droit constitutionnel", "enseignant": "Mme Dupont"}
]
```

Tolérance de 20 min de part et d'autre du créneau.

**4. Sinon Claude déduit** la matière du contenu, en réutilisant en priorité une
matière déjà présente dans le vault.

### Alias

```json
"alias_matieres": { "algo": "Algorithmique", "droit constit": "Droit constitutionnel" }
```

Insensibles aux accents, à la casse et à la ponctuation. Appliqués aux quatre
moyens ci-dessus, y compris à ce que propose Claude — ce qui évite la dérive
« Algorithmique » / « Algorithmes » au fil des semaines.

## Configuration

`~/.local/share/flow2obsidian/config.json` (voir `config.example.json`) :

| Clé | Rôle |
| --- | --- |
| `vault`, `flow_db` | chemins, détectés à l'installation |
| `model` | modèle Claude (`sonnet` par défaut) |
| `min_words` | en deçà, la séance est ignorée |
| `settle_minutes` | délai après la fin avant traitement |
| `max_wait_minutes` | patience maximale avant d'exporter ce qui existe |
| `include_dictations` | joindre les dictées faites pendant la séance |
| `folders` | noms des dossiers dans le vault |

## Fonctionnement interne

- La base Wispr Flow est lue **en lecture seule**, avec copie de repli si elle
  est verrouillée. Elle n'est jamais modifiée.
- L'extraction est **adaptative** : toutes les tables rattachées à la séance sont
  balayées et le texte le plus complet l'emporte, quelle que soit la colonne.
  Le format interne de Wispr peut donc bouger sans casser la chaîne.
- Claude tourne **sans aucun outil** (`--disallowedTools`, MCP désactivé) : c'est
  une transformation de texte, pas un agent lâché sur la machine.
- Claude renvoie du **JSON** ; c'est le code qui produit le Markdown. La mise en
  page est donc identique d'une fiche à l'autre.
- Écritures **atomiques**, verrou d'exécution, état dans SQLite : un passage
  interrompu ne laisse pas de note à moitié écrite et ne double aucun export.

## Limites connues

- **Le démarrage reste un geste.** Wispr Flow ne devine pas qu'un cours commence.
  Le déclenchement par événement de calendrier est le seul chemin vraiment sans
  geste.
- **Le verbatim local n'est pas garanti.** L'audio part en WebSocket vers les
  serveurs de Wispr ; côté local, les notes et le résumé sont certains, le
  transcript intégral dépend de la version de l'app. D'où l'extraction
  adaptative, et `import` comme filet.
- macOS pour l'agent launchd. Ailleurs, une ligne de cron sur
  `flow2obsidian run` suffit.

## Désinstallation

```bash
./uninstall.sh
```

Retire l'agent et la commande. Ne touche ni au vault ni à Wispr Flow.

## Licence

MIT — voir [LICENSE](LICENSE).
