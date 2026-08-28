#!/usr/bin/env python3
"""
flow2obsidian - Wispr Flow  ->  Claude  ->  Obsidian

Detecte les seances enregistrees par le Notetaker de Wispr Flow (ou un
transcript importe a la main), demande a Claude de les structurer, puis
ecrit des notes de cours propres dans le vault Obsidian.

Commandes:
  run                 exporte les seances pretes (mode automatique)
  status              montre ce qui est en attente / deja exporte
  backfill [--all]    exporte aussi les anciennes seances
  import FICHIER      ingere un transcript externe (.txt/.md/.vtt/.srt)
  redo ID             re-exporte une seance
  doctor              verifie l'installation
  selftest            teste toute la chaine sur un cours fictif
"""

import argparse, dataclasses, datetime as dt, fcntl, hashlib, json, os, re
import shutil, sqlite3, subprocess, sys, tempfile, traceback
from pathlib import Path

HOME = Path.home()
BASE = HOME / ".local/share/flow2obsidian"
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "state.sqlite"
LOCK_PATH = BASE / "run.lock"
MARK_START = "<!-- flow2obsidian:debut -->"
MARK_END = "<!-- flow2obsidian:fin -->"

# --------------------------------------------------------------------------
# config / log
# --------------------------------------------------------------------------

DEFAULT_FLOW_DBS = [
    HOME / "Library/Application Support/Wispr Flow/flow.sqlite",          # macOS
    HOME / ".config/Wispr Flow/flow.sqlite",                              # Linux
    Path(os.environ.get("APPDATA", "/nonexistent")) / "Wispr Flow/flow.sqlite",
]

def detect_vault():
    """Retrouve le vault Obsidian ouvert le plus recemment."""
    for cand in (HOME / "Library/Application Support/obsidian/obsidian.json",
                 HOME / ".config/obsidian/obsidian.json",
                 Path(os.environ.get("APPDATA", "/nonexistent")) / "obsidian/obsidian.json"):
        try:
            vaults = json.loads(cand.read_text()).get("vaults", {})
        except (OSError, ValueError):
            continue
        if vaults:
            best = max(vaults.values(), key=lambda v: v.get("ts", 0))
            return best.get("path")
    return None

def detect_flow_db():
    for cand in DEFAULT_FLOW_DBS:
        if cand.exists():
            return str(cand)
    return str(DEFAULT_FLOW_DBS[0])

def load_config():
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"Config absente: {CONFIG_PATH}\nLance ./install.sh depuis le depot.")
    cfg = json.loads(CONFIG_PATH.read_text())
    cfg["vault"] = os.path.expanduser(cfg.get("vault") or detect_vault() or "")
    cfg["flow_db"] = os.path.expanduser(cfg.get("flow_db") or detect_flow_db())
    if not cfg["vault"]:
        raise SystemExit("Aucun vault Obsidian trouve. Renseigne \"vault\" dans "
                         f"{CONFIG_PATH}")
    return cfg

def save_config(cfg):
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    os.replace(tmp, CONFIG_PATH)

LOGFILE = None
def log(msg, level="INFO"):
    line = f"{dt.datetime.now().isoformat(timespec='seconds')} [{level}] {msg}"
    print(line, file=sys.stderr, flush=True)
    if LOGFILE:
        try:
            with open(LOGFILE, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass

def setup_log(cfg):
    global LOGFILE
    meta = Path(cfg["vault"]) / cfg["folders"]["meta"]
    meta.mkdir(parents=True, exist_ok=True)
    LOGFILE = meta / "flow2obsidian.log"

# --------------------------------------------------------------------------
# etat (quelles seances sont deja exportees)
# --------------------------------------------------------------------------

def state_db():
    con = sqlite3.connect(STATE_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS exported(
        meeting_id TEXT PRIMARY KEY,
        note_path  TEXT,
        transcript_path TEXT,
        content_hash TEXT,
        matiere    TEXT,
        titre      TEXT,
        exported_at TEXT,
        status     TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS skipped(
        meeting_id TEXT PRIMARY KEY, raison TEXT, vu_le TEXT)""")
    con.commit()
    return con

# --------------------------------------------------------------------------
# lecture sure de la base Wispr Flow
# --------------------------------------------------------------------------

def open_flow_db(cfg):
    """Ouvre flow.sqlite en lecture seule; copie si la base est verrouillee."""
    src = Path(cfg["flow_db"])
    if not src.exists():
        raise SystemExit(f"Base Wispr Flow introuvable: {src}")
    try:
        con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("SELECT COUNT(*) FROM Meetings").fetchone()
        return con, None
    except sqlite3.Error:
        tmpdir = Path(tempfile.mkdtemp(prefix="flow2obs-"))
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(src) + suffix)
            if p.exists():
                shutil.copy2(p, tmpdir / p.name)
        con = sqlite3.connect(f"file:{tmpdir/src.name}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con, tmpdir

def table_columns(con, table):
    try:
        return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
    except sqlite3.Error:
        return []

def all_tables(con):
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]

# --------------------------------------------------------------------------
# temps
# --------------------------------------------------------------------------

def parse_epoch_ms(v):
    if v in (None, "", 0):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    if n > 10_000_000_000:      # millisecondes
        n //= 1000
    return dt.datetime.fromtimestamp(n)

def parse_dtstr(v):
    if not v:
        return None
    s = str(v).strip().replace("T", " ")
    s = re.sub(r"\s*([+-]\d{2}):?(\d{2})$", "", s)
    s = s.replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            naive = dt.datetime.strptime(s, fmt)
            return naive.replace(tzinfo=dt.timezone.utc).astimezone().replace(tzinfo=None)
        except ValueError:
            continue
    return None

def parse_local(v):
    """Parse une date/heure deja exprimee en heure locale, sans conversion."""
    if isinstance(v, dt.datetime):
        return v
    s = str(v or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

def meeting_times(row):
    start = parse_dtstr(row["createdAt"])
    end = parse_epoch_ms(row["endedAt"]) or parse_dtstr(row["modifiedAt"])
    return start, end

# --------------------------------------------------------------------------
# collecte du materiau
# --------------------------------------------------------------------------

TEXTY = re.compile(r"(text|content|notes|summary|transcript|title|body|speaker|"
                   r"participant|markdown|refined)", re.I)

def gather_meeting_material(con, row, cfg):
    mid = row["id"]
    start, end = meeting_times(row)
    mat = {
        "meeting_id": mid,
        "titre_wispr": row["title"],
        "debut": start.isoformat(sep=" ", timespec="minutes") if start else None,
        "fin": end.isoformat(sep=" ", timespec="minutes") if end else None,
        "participants": row["participantNames"],
        "notes_wispr": row["notes"],
        "resume_wispr": row["summary"],
        "speaker_map": row["speakerMap"],
        "autres_sources": {},
        "dictees_pendant_la_seance": [],
    }

    # tout ce qui est rattache a ce meeting, quelle que soit la table
    for table in all_tables(con):
        if table == "Meetings":
            continue
        cols = table_columns(con, table)
        key = next((c for c in cols if c.lower() in ("meetingid", "meeting_id")), None)
        if not key:
            continue
        try:
            rows = con.execute(
                f'SELECT * FROM "{table}" WHERE "{key}" = ? LIMIT 400', (mid,)).fetchall()
        except sqlite3.Error:
            continue
        blocks = []
        for r in rows:
            piece = {}
            for c in r.keys():
                v = r[c]
                if isinstance(v, str) and v.strip() and (TEXTY.search(c) or len(v) > 40):
                    piece[c] = v[:60000]
            if piece:
                blocks.append(piece)
        if blocks:
            mat["autres_sources"][table] = blocks

    # dictees Wispr faites pendant le creneau (notes perso prises a l'oral)
    if cfg.get("include_dictations") and start and end:
        lo = (start - dt.timedelta(minutes=5))
        hi = (end + dt.timedelta(minutes=15))
        try:
            for r in con.execute(
                    "SELECT timestamp, formattedText, editedText, asrText FROM History "
                    "WHERE isArchived = 0 ORDER BY timestamp").fetchall():
                ts = parse_dtstr(r["timestamp"])
                if ts and lo <= ts <= hi:
                    txt = r["editedText"] or r["formattedText"] or r["asrText"]
                    if txt and txt.strip():
                        mat["dictees_pendant_la_seance"].append(
                            {"heure": ts.strftime("%H:%M"), "texte": txt.strip()})
        except sqlite3.Error:
            pass
    return mat

def best_transcript(mat):
    """Choisit le texte le plus proche d'un verbatim parmi tout le materiau."""
    candidates = []
    for table, blocks in mat.get("autres_sources", {}).items():
        for b in blocks:
            for col, val in b.items():
                score = len(val)
                if re.search(r"transcript", col, re.I) or re.search(r"transcript", table, re.I):
                    score *= 4
                elif re.search(r"content|text|body", col, re.I):
                    score *= 2
                candidates.append((score, f"{table}.{col}", val))
    if mat.get("notes_wispr"):
        candidates.append((len(mat["notes_wispr"]) * 2, "Meetings.notes", mat["notes_wispr"]))
    if mat.get("dictees_pendant_la_seance"):
        joined = "\n".join(f"[{d['heure']}] {d['texte']}"
                           for d in mat["dictees_pendant_la_seance"])
        candidates.append((len(joined), "dictees", joined))
    if not candidates:
        return "", "aucune"
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][2], candidates[0][1]

def word_count(txt):
    return len(re.findall(r"\S+", txt or ""))

# --------------------------------------------------------------------------
# resolution du nom de la matiere
# --------------------------------------------------------------------------

import unicodedata

JOURS = {"lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3,
         "vendredi": 4, "samedi": 5, "dimanche": 6}

SEPARATEURS = ["\u2014", "\u2013", " : ", " | ", " / ", " - ", ">"]

def norm_key(s):
    """Cle de comparaison: sans accents, minuscules, sans ponctuation."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return s.strip()

def alias_index(cfg):
    """Table de correspondance -> nom canonique de matiere."""
    idx = {}
    for m in cfg.get("matieres_connues") or []:
        idx[norm_key(m)] = m
    for k, v in (cfg.get("alias_matieres") or {}).items():
        idx[norm_key(k)] = v
        idx[norm_key(v)] = v
    return idx

def canonise(cfg, nom):
    """Ramene un nom de matiere a sa forme canonique si on la connait."""
    if not nom:
        return None
    return alias_index(cfg).get(norm_key(nom), str(nom).strip())

def split_titre(cfg, titre):
    """'Droit constit - Hierarchie des normes' -> ('Droit constitutionnel',
    'Hierarchie des normes') si la partie gauche est une matiere connue."""
    if not titre:
        return None, None
    idx = alias_index(cfg)
    for sep in SEPARATEURS:
        if sep in titre:
            gauche, droite = titre.split(sep, 1)
            gauche, droite = gauche.strip(), droite.strip()
            connue = idx.get(norm_key(gauche))
            if connue:
                return connue, (droite or None)
            if cfg.get("titre_toujours_separe"):
                return gauche, (droite or None)
    return None, None

def hhmm(v):
    try:
        h, m = str(v).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None

def creneau_emploi_du_temps(cfg, debut):
    """Retrouve la matiere d'apres le jour et l'heure de l'enregistrement."""
    if not debut:
        return None
    when = parse_local(debut)
    if not when:
        return None
    marge = int(cfg.get("marge_emploi_du_temps_min", 20))
    minute = when.hour * 60 + when.minute
    for c in cfg.get("emploi_du_temps") or []:
        jours = [JOURS.get(norm_key(j)) for j in as_list(c.get("jours") or c.get("jour"))]
        if when.weekday() not in [j for j in jours if j is not None]:
            continue
        d0, d1 = hhmm(c.get("debut")), hhmm(c.get("fin"))
        if d0 is None or d1 is None:
            continue
        if d0 - marge <= minute <= d1 + marge:
            return c
    return None

def resolve_matiere(cfg, mat):
    """Ordre de priorite: option explicite > titre Wispr > emploi du temps.
    Renvoie (matiere, titre, enseignant, origine) - chaque champ peut etre None."""
    if mat.get("matiere_imposee"):
        return (canonise(cfg, mat["matiere_imposee"]), mat.get("titre_impose"),
                None, "option explicite")
    m, t = split_titre(cfg, mat.get("titre_wispr"))
    if m:
        return m, (t or mat.get("titre_impose")), None, "titre de l'enregistrement"
    creneau = creneau_emploi_du_temps(cfg, mat.get("debut"))
    if creneau:
        return (canonise(cfg, creneau.get("matiere")), mat.get("titre_impose"),
                creneau.get("enseignant"), "emploi du temps")
    return None, mat.get("titre_impose"), None, None

# --------------------------------------------------------------------------
# appel a Claude
# --------------------------------------------------------------------------

SYSTEM = """Tu es un assistant qui transforme la captation audio d'un cours en \
fiche de revision structuree. Tu es rigoureux: tu ne inventes jamais de contenu \
absent du transcript. Si un passage est inaudible ou ambigu, tu le signales dans \
"lacunes" plutot que de combler le trou. Tu ecris en francais correct et PLEINEMENT ACCENTUE (e accent aigu, \
accent grave, cedille, circonflexe), dans un style dense et clair, adapte a la \
revision."""

SCHEMA_KEYS = ["matiere", "titre", "date_seance", "enseignant", "type",
               "resume_court", "plan", "notions_cles", "points_importants",
               "a_reviser", "questions_examen", "taches", "references",
               "liens_obsidian", "tags", "confiance", "lacunes"]

def build_prompt(mat, transcript, cfg, date_hint, impose=None):
    impose = impose or {}
    connues = cfg.get("matieres_connues") or []
    connues_txt = ("\nMatieres deja presentes dans le vault (reutilise EXACTEMENT "
                   "l'une d'elles si le cours en releve, sinon cree un nom neuf): "
                   + ", ".join(f'"{m}"' for m in connues)) if connues else ""
    impose_matiere = (
        f'la matiere est IMPOSEE, recopie exactement cette valeur : '
        f'"{impose["matiere"]}"' if impose.get("matiere")
        else 'nom court de la matiere/du cours (ex: "Droit constitutionnel").')
    impose_titre = (
        f'le titre est IMPOSE, recopie exactement cette valeur : "{impose["titre"]}"'
        if impose.get("titre")
        else 'titre de la seance, precis, sans date (ex: "La hierarchie des normes")')
    if impose.get("matiere"):
        connues_txt = ""
    return f"""Voici la captation d'une seance (cours, TD, conference ou reunion).

Produis UNIQUEMENT un objet JSON valide, sans texte autour, sans bloc de code.\nTout le texte que tu ecris doit etre en francais correctement accentue.

Champs attendus:
- "matiere": {impose_matiere}{connues_txt}
- "titre": {impose_titre}
- "date_seance": "YYYY-MM-DD" (utilise {date_hint} si le transcript ne dit rien)
- "enseignant": nom si mentionne, sinon null
- "type": "cours magistral" | "TD" | "TP" | "conference" | "reunion" | "autre"
- "resume_court": 2 a 4 phrases, l'essentiel de la seance
- "plan": tableau d'objets {{"titre": "...", "contenu": "..."}} qui suit le
  deroule reel du cours; "contenu" en markdown, avec des puces, plusieurs
  phrases par section, en gardant les exemples et les chiffres donnes
- "notions_cles": tableau de {{"terme": "...", "definition": "..."}}
- "points_importants": tableau de phrases (ce que le prof a insiste/repete)
- "a_reviser": tableau de points a retravailler
- "questions_examen": tableau de questions d'examen plausibles sur cette seance
- "taches": tableau de CHAINES de caracteres (pas d'objets) decrivant ce qu'il
  y a a faire (devoirs, lectures, rendus), en integrant l'echeance dans la phrase
- "references": tableau de CHAINES (livres, articles, arrets, auteurs, liens cites)
- "liens_obsidian": tableau de noms de notes a lier (concepts majeurs)
- "tags": tableau de tags kebab-case sans '#'
- "confiance": nombre entre 0 et 1 sur la qualite de la captation
- "lacunes": tableau des passages incertains ou manquants

Metadonnees de la seance:
{json.dumps({k: v for k, v in mat.items()
             if k not in ("autres_sources",)}, ensure_ascii=False, indent=1)[:6000]}

Transcript / contenu capte:
<<<TRANSCRIPT
{transcript[:400000]}
TRANSCRIPT>>>
"""

def extract_json(s):
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.S)
    start = s.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None

def call_claude(prompt, cfg, timeout=900):
    cmd = [cfg.get("claude_bin") or shutil.which("claude") or "claude", "-p",
           "--model", cfg.get("model", "sonnet"),
           "--output-format", "text",
           "--append-system-prompt", SYSTEM,
           "--disallowedTools",
           "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,TodoWrite,NotebookEdit",
           "--disable-slash-commands",
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
    extra = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
             str(HOME / ".local/bin"), str(HOME / "bin")]
    path = os.pathsep.join(dict.fromkeys(
        os.environ.get("PATH", "").split(os.pathsep) + extra))
    env = dict(os.environ, PATH=path)
    res = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                         timeout=timeout, cwd=str(BASE), env=env)
    if res.returncode != 0:
        raise RuntimeError(f"claude a echoue ({res.returncode}): {res.stderr[:800]}")
    return res.stdout

def analyse(mat, transcript, cfg, date_hint, impose=None):
    prompt = build_prompt(mat, transcript, cfg, date_hint, impose)
    out = call_claude(prompt, cfg)
    data = extract_json(out)
    if data is None:
        log("JSON invalide, seconde tentative de reparation", "WARN")
        repair = ("La reponse suivante devait etre un objet JSON valide et ne "
                  "l'est pas. Renvoie UNIQUEMENT le JSON corrige, rien d'autre.\n\n"
                  + out[:120000])
        data = extract_json(call_claude(repair, cfg, timeout=300))
    if data is None:
        raise RuntimeError("Claude n'a pas renvoye de JSON exploitable")
    for k in SCHEMA_KEYS:
        data.setdefault(k, None)
    return data

# --------------------------------------------------------------------------
# ecriture Obsidian
# --------------------------------------------------------------------------

def sanitize(name, fallback="Sans titre"):
    name = (name or "").strip()
    name = re.sub(r'[\\/:*?"<>|#\^\[\]]', "-", name)
    name = re.sub(r"\s+", " ", name).strip(" .-")
    return name[:90] or fallback

def yaml_str(v):
    if v is None:
        return "null"
    s = str(v).replace('"', "'")
    return f'"{s}"'

def yaml_list(items):
    items = [i for i in (items or []) if i]
    if not items:
        return "[]"
    return "[" + ", ".join(yaml_str(str(i)) for i in items) + "]"

def as_text(item):
    """Rend lisible un element de liste, qu'il soit une chaine ou un objet."""
    if item is None:
        return ""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for k in ("tache", "titre", "texte", "question", "point", "terme",
                  "reference", "libelle", "nom", "label", "description"):
            if item.get(k):
                main = str(item[k]).strip()
                rest = [f"{kk} : {vv}" for kk, vv in item.items()
                        if kk != k and vv not in (None, "", [], {})]
                return main + (f" ({'; '.join(str(r) for r in rest)})" if rest else "")
        return " — ".join(f"{k} : {v}" for k, v in item.items() if v)
    return str(item)

def as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]

def write_atomic(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)

def render_note(d, mat, transcript_link, duree_min, source_col):
    matiere = sanitize(d.get("matiere"), "Divers")
    titre = sanitize(d.get("titre"), "Seance")
    date = d.get("date_seance") or dt.date.today().isoformat()
    tags = ["cours"] + [re.sub(r"[^a-z0-9-]", "", t.lower().replace(" ", "-"))
                        for t in as_list(d.get("tags"))]
    L = []
    L.append("---")
    L.append("type: cours")
    L.append(f"matiere: {yaml_str(matiere)}")
    L.append(f"titre: {yaml_str(titre)}")
    L.append(f"date: {date}")
    L.append(f"enseignant: {yaml_str(d.get('enseignant'))}")
    L.append(f"seance: {yaml_str(d.get('type'))}")
    L.append(f"duree_min: {duree_min if duree_min else 'null'}")
    L.append(f"confiance: {d.get('confiance') if isinstance(d.get('confiance'), (int, float)) else 'null'}")
    L.append(f"source: {yaml_str('wispr-flow/' + str(source_col))}")
    L.append(f"meeting_id: {yaml_str(mat.get('meeting_id'))}")
    L.append(f"tags: {yaml_list(sorted(set(t for t in tags if t)))}")
    L.append("genere_par: flow2obsidian")
    L.append("---")
    L.append("")
    L.append(f"# {titre}")
    L.append("")
    L.append(f"> [!abstract] En bref")
    for line in (d.get("resume_court") or "").splitlines() or [""]:
        L.append(f"> {line}")
    L.append("")
    L.append(f"**Matière** : [[{matiere}]] · **Date** : {date}"
             + (f" · **Enseignant** : {d['enseignant']}" if d.get("enseignant") else ""))
    if transcript_link:
        L.append(f"**Transcript complet** : [[{transcript_link}]]")
    L.append("")

    plan = as_list(d.get("plan"))
    if plan:
        L.append("## Déroulé du cours")
        L.append("")
        for sec in plan:
            if isinstance(sec, dict):
                L.append(f"### {sec.get('titre', 'Section')}")
                L.append("")
                L.append(str(sec.get("contenu", "")).strip())
            else:
                L.append(str(sec))
            L.append("")

    notions = as_list(d.get("notions_cles"))
    if notions:
        L.append("## Notions clés")
        L.append("")
        for n in notions:
            if isinstance(n, dict) and n.get("terme"):
                L.append(f"- **{n['terme']}** — {n.get('definition','')}")
            else:
                L.append(f"- {as_text(n)}")
        L.append("")

    for key, header, prefix in (
            ("points_importants", "## À retenir", "- "),
            ("a_reviser", "## À retravailler", "- [ ] "),
            ("questions_examen", "## Questions d'examen probables", "- "),
            ("references", "## Références citées", "- ")):
        items = as_list(d.get(key))
        if items:
            L.append(header)
            L.append("")
            for it in items:
                txt = as_text(it)
                if txt:
                    L.append(f"{prefix}{txt}")
            L.append("")

    taches = as_list(d.get("taches"))
    if taches:
        L.append("## À faire")
        L.append("")
        for t in taches:
            txt = as_text(t)
            if txt:
                L.append(f"- [ ] {txt}")
        L.append("")

    liens = [sanitize(as_text(x)) for x in as_list(d.get("liens_obsidian")) if x]
    if liens:
        L.append("## Concepts liés")
        L.append("")
        L.append(" · ".join(f"[[{x}]]" for x in liens))
        L.append("")

    lac = as_list(d.get("lacunes"))
    if lac:
        L.append("> [!warning] Zones incertaines de la captation")
        for x in lac:
            L.append(f"> - {as_text(x)}")
        L.append("")
    return "\n".join(L).rstrip() + "\n", matiere, titre, date

def render_transcript(mat, transcript, matiere, titre, date, note_link, source_col):
    L = ["---", "type: transcript", f"matiere: {yaml_str(matiere)}",
         f"date: {date}", f"source: {yaml_str('wispr-flow/' + str(source_col))}",
         f"meeting_id: {yaml_str(mat.get('meeting_id'))}",
         "tags: [\"transcript\"]", "genere_par: flow2obsidian", "---", "",
         f"# Transcript — {titre}", "", f"Fiche de cours : [[{note_link}]]", ""]
    if mat.get("debut"):
        L.append(f"Début : {mat['debut']}  ·  Fin : {mat.get('fin') or '?'}")
        L.append("")
    if mat.get("participants"):
        L.append(f"Participants : {mat['participants']}")
        L.append("")
    L.append("---")
    L.append("")
    L.append(transcript.strip())
    dictees = mat.get("dictees_pendant_la_seance") or []
    if dictees and source_col != "dictees":
        L += ["", "## Mes notes dictées pendant la séance", ""]
        for d in dictees:
            L.append(f"- **{d['heure']}** — {d['texte']}")
    return "\n".join(L).rstrip() + "\n"

def update_moc(cfg, matiere):
    """Regenere la note MOC d'une matiere, en preservant ce que l'utilisateur
    a ecrit hors des marqueurs."""
    vault = Path(cfg["vault"])
    coursdir = vault / cfg["folders"]["cours"] / matiere
    mocpath = vault / cfg["folders"]["matieres"] / f"{matiere}.md"
    entries = []
    for f in sorted(coursdir.glob("*.md")):
        txt = f.read_text(encoding="utf-8", errors="replace")
        date = re.search(r"^date:\s*(\S+)", txt, re.M)
        titre = re.search(r'^titre:\s*"?([^"\n]+)"?', txt, re.M)
        resume = re.search(r"^> \[!abstract\][^\n]*\n((?:> .*\n)+)", txt, re.M)
        short = ""
        if resume:
            short = " ".join(l[2:].strip() for l in resume.group(1).splitlines())[:160]
        entries.append((date.group(1) if date else "0000-00-00",
                        titre.group(1).strip() if titre else f.stem, f.stem, short))
    entries.sort(reverse=True)

    block = [MARK_START,
             f"*{len(entries)} séance(s) · mis à jour le "
             f"{dt.date.today().isoformat()}*", "",
             "| Date | Séance | En bref |", "| --- | --- | --- |"]
    for date, titre, stem, short in entries:
        block.append(f"| {date} | [[{stem}\\|{titre}]] | {short} |")
    block += ["", "## Toutes les notes de la matière",
              f"```dataview\nTABLE date AS Date, seance AS Type\n"
              f'FROM "{cfg["folders"]["cours"]}/{matiere}"\nSORT date DESC\n```',
              MARK_END]
    block_txt = "\n".join(block)

    if mocpath.exists():
        old = mocpath.read_text(encoding="utf-8")
        if MARK_START in old and MARK_END in old:
            new = re.sub(re.escape(MARK_START) + r".*?" + re.escape(MARK_END),
                         block_txt.replace("\\", "\\\\"), old, flags=re.S)
        else:
            new = old.rstrip() + "\n\n" + block_txt + "\n"
    else:
        new = (f"---\ntype: matiere\nmatiere: {yaml_str(matiere)}\n"
               f'tags: ["matiere"]\n---\n\n# {matiere}\n\n'
               f"> Notes personnelles libres au-dessus de cette ligne.\n\n"
               + block_txt + "\n")
    write_atomic(mocpath, new)

def update_journal(cfg, date, matiere, note_stem, titre):
    jpath = Path(cfg["vault"]) / cfg["folders"]["journal"] / f"{date}.md"
    link = f"- [[{note_stem}\\|{titre}]] — [[{matiere}]]"
    if jpath.exists():
        txt = jpath.read_text(encoding="utf-8")
        if note_stem in txt:
            return
        if "## Cours du jour" in txt:
            txt = txt.replace("## Cours du jour", "## Cours du jour\n" + link, 1)
        else:
            txt = txt.rstrip() + "\n\n## Cours du jour\n" + link + "\n"
    else:
        txt = (f"---\ntype: journal\ndate: {date}\ntags: [\"journal\"]\n---\n\n"
               f"# {date}\n\n## Cours du jour\n{link}\n")
    write_atomic(jpath, txt)

# --------------------------------------------------------------------------
# pipeline d'export
# --------------------------------------------------------------------------

def export_material(cfg, st, mat, transcript, source_col, duree_min, force=False):
    mid = mat["meeting_id"]
    h = hashlib.sha256(transcript.encode("utf-8", "replace")).hexdigest()[:16]
    row = st.execute("SELECT content_hash, note_path FROM exported WHERE meeting_id=?",
                     (mid,)).fetchone()
    if row and row[0] == h and not force:
        log(f"deja exporte, inchange: {mid}")
        return None

    date_hint = (mat.get("debut") or "")[:10] or dt.date.today().isoformat()
    f_mat, f_titre, f_ens, origine = resolve_matiere(cfg, mat)
    if origine:
        log(f"matiere fixee par {origine} : {f_mat or '(titre seul)'}")
    impose = {"matiere": f_mat, "titre": f_titre}
    log(f"analyse par Claude ({cfg.get('model')}) — {word_count(transcript)} mots")
    d = analyse(mat, transcript, cfg, date_hint, impose)

    # les valeurs imposees l'emportent sur ce que Claude a devine
    if f_mat:
        d["matiere"] = f_mat
    else:
        d["matiere"] = canonise(cfg, d.get("matiere"))
    if f_titre:
        d["titre"] = f_titre
    if f_ens and not d.get("enseignant"):
        d["enseignant"] = f_ens
    if mat.get("date_imposee"):
        d["date_seance"] = mat["date_imposee"]

    vault = Path(cfg["vault"])
    note_md, matiere, titre, date = render_note(d, mat, None, duree_min, source_col)
    stem = f"{date} {titre}"
    tstem = f"{date} {titre} — transcript"
    note_path = vault / cfg["folders"]["cours"] / matiere / f"{stem}.md"
    tpath = vault / cfg["folders"]["transcripts"] / matiere / f"{tstem}.md"

    note_md, _, _, _ = render_note(d, mat, tstem, duree_min, source_col)
    write_atomic(note_path, note_md)
    write_atomic(tpath, render_transcript(mat, transcript, matiere, titre, date,
                                          stem, source_col))
    if cfg.get("keep_raw"):
        raw = vault / cfg["folders"]["meta"] / "captures-brutes" / f"{mid}.json"
        write_atomic(raw, json.dumps({"materiau": mat, "analyse": d},
                                     ensure_ascii=False, indent=1))
    update_moc(cfg, matiere)
    update_journal(cfg, date, matiere, stem, titre)

    if matiere not in (cfg.get("matieres_connues") or []):
        cfg.setdefault("matieres_connues", []).append(matiere)
        save_config(cfg)

    st.execute("REPLACE INTO exported VALUES(?,?,?,?,?,?,?,?)",
               (mid, str(note_path), str(tpath), h, matiere, titre,
                dt.datetime.now().isoformat(timespec="seconds"), "ok"))
    st.commit()
    log(f"ecrit: {note_path}")
    return note_path

def ready_meetings(con, cfg, backfill=False, only_id=None):
    now = dt.datetime.now()
    out = []
    for row in con.execute("SELECT * FROM Meetings WHERE isDeleted = 0 "
                           "ORDER BY createdAt").fetchall():
        if only_id and row["id"] != only_id:
            continue
        start, end = meeting_times(row)
        if not end:
            if not only_id:
                continue
            end = start or dt.datetime.now()
        age = (now - end).total_seconds() / 60
        if not only_id:
            if age < cfg["settle_minutes"]:
                continue
            has_content = bool((row["notes"] or "").strip() or (row["summary"] or "").strip())
            if not has_content and age < cfg["max_wait_minutes"]:
                continue
            if not backfill and age > 60 * 24 * 7:
                continue
        out.append(row)
    return out

def cmd_run(cfg, args):
    st = state_db()
    con, tmpdir = open_flow_db(cfg)
    try:
        rows = ready_meetings(con, cfg, backfill=args.all if hasattr(args, "all") else False,
                              only_id=getattr(args, "meeting_id", None))
        if not rows:
            log("aucune seance prete")
            return 0
        done = 0
        for row in rows:
            mid = row["id"]
            if not getattr(args, "force", False):
                if st.execute("SELECT 1 FROM exported WHERE meeting_id=?",
                              (mid,)).fetchone():
                    continue
            mat = gather_meeting_material(con, row, cfg)
            if getattr(args, "matiere", None):
                mat["matiere_imposee"] = args.matiere
            if getattr(args, "titre", None):
                mat["titre_impose"] = args.titre
            transcript, source_col = best_transcript(mat)
            wc = word_count(transcript)
            if wc < cfg["min_words"] and not getattr(args, "force", False):
                st.execute("REPLACE INTO skipped VALUES(?,?,?)",
                           (mid, f"trop court ({wc} mots)",
                            dt.datetime.now().isoformat(timespec="seconds")))
                st.commit()
                log(f"ignore (trop court, {wc} mots): {row['title']}")
                continue
            start, end = meeting_times(row)
            duree = int((end - start).total_seconds() / 60) if start and end else None
            try:
                if export_material(cfg, st, mat, transcript, source_col, duree,
                                   force=getattr(args, "force", False)):
                    done += 1
            except Exception as e:
                log(f"echec sur {mid}: {e}", "ERROR")
                log(traceback.format_exc(), "ERROR")
                st.execute("REPLACE INTO exported VALUES(?,?,?,?,?,?,?,?)",
                           (mid, None, None, None, None, row["title"],
                            dt.datetime.now().isoformat(timespec="seconds"),
                            f"erreur: {e}"))
                st.commit()
        log(f"termine, {done} note(s) creee(s)")
        return 0
    finally:
        con.close()
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)

# --------------------------------------------------------------------------
# import manuel d'un transcript externe
# --------------------------------------------------------------------------

def strip_subtitles(txt):
    txt = re.sub(r"^\d+\s*$", "", txt, flags=re.M)
    txt = re.sub(r"^\d{2}:\d{2}:\d{2}[.,]\d+\s*-->.*$", "", txt, flags=re.M)
    txt = re.sub(r"^WEBVTT.*$", "", txt, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", txt).strip()

def cmd_import(cfg, args):
    src = Path(os.path.expanduser(args.fichier))
    txt = src.read_text(encoding="utf-8", errors="replace")
    if src.suffix.lower() in (".vtt", ".srt"):
        txt = strip_subtitles(txt)
    when = args.date or dt.datetime.fromtimestamp(src.stat().st_mtime).date().isoformat()
    mid = "import-" + hashlib.sha256(
        (str(src) + txt[:2000]).encode()).hexdigest()[:12]
    mat = {"meeting_id": mid, "titre_wispr": args.titre or src.stem,
           "debut": f"{when} 00:00", "fin": None, "participants": None,
           "notes_wispr": None, "resume_wispr": None, "speaker_map": None,
           "autres_sources": {}, "dictees_pendant_la_seance": []}
    if args.matiere:
        mat["matiere_imposee"] = args.matiere
    if getattr(args, "titre", None):
        mat["titre_impose"] = args.titre
    if args.date:
        mat["date_imposee"] = args.date
    st = state_db()
    p = export_material(cfg, st, mat, txt, f"import:{src.name}",
                        args.duree, force=True)
    print(p or "rien a faire")
    return 0

# --------------------------------------------------------------------------
# status / doctor / selftest
# --------------------------------------------------------------------------

def cmd_status(cfg, args):
    st = state_db()
    con, tmpdir = open_flow_db(cfg)
    try:
        total = con.execute("SELECT COUNT(*) FROM Meetings WHERE isDeleted=0").fetchone()[0]
        print(f"Seances Wispr Flow en base : {total}")
        rows = st.execute("SELECT exported_at, matiere, titre, status FROM exported "
                          "ORDER BY exported_at DESC LIMIT 20").fetchall()
        print(f"Notes exportees : {len(st.execute('SELECT 1 FROM exported').fetchall())}")
        for r in rows:
            print(f"  {r[0]}  [{r[3]}]  {r[1] or '?'} — {r[2] or '?'}")
        sk = st.execute("SELECT meeting_id, raison FROM skipped").fetchall()
        if sk:
            print(f"Ignorees : {len(sk)}")
            for r in sk:
                print(f"  {r[0]} — {r[1]}")
    finally:
        con.close()
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return 0

def cmd_doctor(cfg, args):
    ok = True
    def chk(label, cond, extra=""):
        nonlocal ok
        print(f"  [{'OK ' if cond else 'KO '}] {label} {extra}")
        ok = ok and cond
    print("flow2obsidian — diagnostic\n")
    chk("vault Obsidian", Path(cfg["vault"]).is_dir(), cfg["vault"])
    chk("base Wispr Flow", Path(cfg["flow_db"]).exists(), cfg["flow_db"])
    cb = cfg.get("claude_bin") or shutil.which("claude") or "claude"
    chk("CLI claude", shutil.which(cb) is not None or Path(cb).exists(), cb)
    plist = HOME / "Library/LaunchAgents/com.flow2obsidian.agent.plist"
    chk("agent launchd installe", plist.exists(), str(plist))
    try:
        loaded = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
        chk("agent launchd charge", "com.flow2obsidian.agent" in loaded.stdout)
    except Exception:
        chk("agent launchd charge", False)
    try:
        con, tmpdir = open_flow_db(cfg)
        n = con.execute("SELECT COUNT(*) FROM Meetings").fetchone()[0]
        con.close()
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        chk("lecture de flow.sqlite", True, f"{n} seance(s)")
    except Exception as e:
        chk("lecture de flow.sqlite", False, str(e))
    print()
    return 0 if ok else 1

SELFTEST_TRANSCRIPT = """Bonjour a tous, asseyez-vous. Aujourd'hui on attaque le
chapitre trois, la hierarchie des normes. C'est un point central, il tombera
probablement a l'examen de janvier.

Alors, la hierarchie des normes, c'est l'idee formulee par Hans Kelsen dans la
Theorie pure du droit, en 1934. Kelsen propose de se representer l'ordre
juridique comme une pyramide. Chaque norme tire sa validite de la norme qui lui
est superieure. Au sommet, ce que Kelsen appelle la norme fondamentale, la
Grundnorm.

En droit francais, la pyramide se lit ainsi : le bloc de constitutionnalite tout
en haut, puis les traites internationaux et le droit de l'Union, puis la loi,
puis les reglements, et enfin les actes administratifs individuels. Retenez bien
cet ordre, je le redemande systematiquement.

Le bloc de constitutionnalite, attention, ce n'est pas seulement la Constitution
de 1958. C'est aussi le preambule de 1946, la Declaration de 1789, et les
principes fondamentaux reconnus par les lois de la Republique. C'est la decision
Liberte d'association du 16 juillet 1971 qui consacre cela.

Sur la place des traites, l'article 55 de la Constitution pose leur superiorite
sur la loi, sous reserve de reciprocite. L'arret Jacques Vabre de 1975 pour la
Cour de cassation, et l'arret Nicolo de 1989 pour le Conseil d'Etat, acceptent
de faire prevaloir le traite sur une loi posterieure.

Pour la semaine prochaine, vous me lisez le commentaire de l'arret Nicolo dans
le Grand Arrets, pages 220 a 235. Et vous preparez une fiche sur la difference
entre controle de constitutionnalite et controle de conventionnalite. C'est a
rendre le 12.
"""

def cmd_selftest(cfg, args):
    log("selftest: cours fictif de droit constitutionnel")
    mat = {"meeting_id": "selftest-0001",
           "titre_wispr": "Amphi droit constit",
           "debut": dt.datetime.now().replace(microsecond=0).isoformat(sep=" ", timespec="minutes"),
           "fin": None, "participants": None, "notes_wispr": None,
           "resume_wispr": None, "speaker_map": None,
           "autres_sources": {}, "dictees_pendant_la_seance": []}
    st = state_db()
    p = export_material(cfg, st, mat, SELFTEST_TRANSCRIPT, "selftest", 62, force=True)
    print(f"\nNote generee : {p}")
    return 0

# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(prog="flow2obsidian")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("run"); p.add_argument("--force", action="store_true")
    p.add_argument("--all", action="store_true")
    sub.add_parser("status")
    p = sub.add_parser("backfill"); p.add_argument("--all", action="store_true", default=True)
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("redo"); p.add_argument("meeting_id")
    p.add_argument("--matiere"); p.add_argument("--titre")
    p.add_argument("--force", action="store_true", default=True)
    p.add_argument("--all", action="store_true", default=True)
    p = sub.add_parser("import")
    p.add_argument("fichier")
    p.add_argument("--titre"); p.add_argument("--matiere")
    p.add_argument("--date"); p.add_argument("--duree", type=int)
    sub.add_parser("doctor")
    sub.add_parser("selftest")
    args = ap.parse_args()
    cmd = args.cmd or "run"

    cfg = load_config()
    setup_log(cfg)

    if cmd in ("run", "backfill", "redo", "import", "selftest"):
        BASE.mkdir(parents=True, exist_ok=True)
        lock = open(LOCK_PATH, "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("une autre execution est en cours, on sort")
            return 0

    return {
        "run": cmd_run, "backfill": cmd_run, "redo": cmd_run,
        "import": cmd_import, "status": cmd_status,
        "doctor": cmd_doctor, "selftest": cmd_selftest,
    }[cmd](cfg, args)

if __name__ == "__main__":
    sys.exit(main() or 0)
