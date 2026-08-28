#!/bin/bash
# Installe flow2obsidian : script, config, wrapper CLI et agent launchd.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="$HOME/.local/share/flow2obsidian"
LABEL="com.flow2obsidian.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

say() { printf '  %s\n' "$*"; }

echo
echo "flow2obsidian — installation"
echo

# --- prerequis -------------------------------------------------------------
command -v python3 >/dev/null || { echo "python3 est requis." >&2; exit 1; }
if ! command -v claude >/dev/null; then
  echo "Le CLI 'claude' est introuvable dans le PATH." >&2
  echo "Installe Claude Code puis relance ce script." >&2
  exit 1
fi

# --- emplacement du binaire ------------------------------------------------
if [ -d "$HOME/.local/bin" ]; then BIN="$HOME/.local/bin"
elif [ -d "$HOME/bin" ];        then BIN="$HOME/bin"
else BIN="$HOME/.local/bin"; mkdir -p "$BIN"
fi

mkdir -p "$BASE"
cp "$REPO/flow2obsidian.py" "$BASE/flow2obsidian.py"
say "script      $BASE/flow2obsidian.py"

# --- config ----------------------------------------------------------------
if [ -f "$BASE/config.json" ]; then
  say "config      conservée (existante)"
else
  cp "$REPO/config.example.json" "$BASE/config.json"
  python3 - "$BASE/config.json" <<'PY'
import json, sys, pathlib, os
p = pathlib.Path(sys.argv[1]); cfg = json.loads(p.read_text())
home = pathlib.Path.home()
for cand in (home/"Library/Application Support/obsidian/obsidian.json",
             home/".config/obsidian/obsidian.json"):
    try:
        v = json.loads(cand.read_text()).get("vaults", {})
    except (OSError, ValueError):
        continue
    if v:
        cfg["vault"] = max(v.values(), key=lambda x: x.get("ts", 0)).get("path")
        break
for cand in (home/"Library/Application Support/Wispr Flow/flow.sqlite",
             home/".config/Wispr Flow/flow.sqlite"):
    if cand.exists():
        cfg["flow_db"] = str(cand); break
p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
print("  vault       " + str(cfg.get("vault") or "NON DÉTECTÉ — à renseigner"))
print("  base Wispr  " + str(cfg.get("flow_db") or "NON DÉTECTÉE — à renseigner"))
PY
fi

# --- wrapper CLI -----------------------------------------------------------
cat > "$BIN/flow2obsidian" <<'WRAP'
#!/bin/sh
exec python3 "$HOME/.local/share/flow2obsidian/flow2obsidian.py" "$@"
WRAP
chmod +x "$BIN/flow2obsidian"
say "commande    $BIN/flow2obsidian"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) say "ATTENTION  $BIN n'est pas dans ton PATH — ajoute-le à ton profil." ;;
esac

# --- arborescence du vault -------------------------------------------------
python3 - "$BASE/config.json" <<'PY'
import json, sys, pathlib
cfg = json.loads(pathlib.Path(sys.argv[1]).read_text())
v = cfg.get("vault")
if v:
    root = pathlib.Path(v).expanduser()
    for f in cfg["folders"].values():
        (root/f).mkdir(parents=True, exist_ok=True)
    (root/cfg["folders"]["meta"]/"captures-brutes").mkdir(parents=True, exist_ok=True)
    print("  dossiers    créés dans le vault")
PY

# --- agent launchd (macOS) -------------------------------------------------
if [ "$(uname)" = "Darwin" ]; then
  FLOWDB="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("flow_db") or "")' "$BASE/config.json")"
  if [ -n "$FLOWDB" ]; then
    mkdir -p "$HOME/Library/LaunchAgents"
    sed -e "s|__WRAPPER__|$BIN/flow2obsidian|g" \
        -e "s|__HOME__|$HOME|g" \
        -e "s|__BASE__|$BASE|g" \
        -e "s|__PATH__|$(dirname "$(command -v claude)"):/usr/bin:/bin:/usr/sbin:/sbin:$BIN|g" \
        -e "s|__FLOWWAL__|$FLOWDB-wal|g" \
        "$REPO/agent.plist.template" > "$PLIST"
    plutil -lint "$PLIST" >/dev/null
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    say "agent       chargé ($LABEL)"
  else
    say "agent       ignoré (base Wispr Flow non détectée)"
  fi
else
  say "agent       launchd ignoré (hors macOS) — utilise cron :"
  say "            */10 * * * * $BIN/flow2obsidian run"
fi

echo
"$BIN/flow2obsidian" doctor || true
echo "Terminé. Essaie :  flow2obsidian selftest"
echo
