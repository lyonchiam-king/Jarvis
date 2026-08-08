#!/usr/bin/env bash
# SME Outreach - menu launcher for macOS and Linux.
# Make it double-clickable on a Mac:  chmod +x run.sh  (then rename to run.command)
set -uo pipefail
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/outreach" ]; then
    echo
    echo "  First time here -- setting things up. This takes a minute or two."
    echo
    python3 -m venv .venv || {
        echo "  ERROR: Python 3.10+ not found. Install it and try again."
        exit 1
    }
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install --quiet -e ".[dev]"
    echo "  Setup done."
fi

OUTREACH=".venv/bin/outreach"

pause() {
    echo
    echo "  ------------------------------------------------"
    read -rsn1 -p "  Done. Press any key to go back to the menu."
    echo
}

while true; do
    clear
    cat <<'MENU'

  ============================================
     SME OUTREACH
  ============================================

     GATHER
       1.  Find new leads
       2.  Research the leads I have

     PREPARE
       3.  Write website prompts
       4.  Draft my outreach messages

     ALL OF IT
       5.  Run the full pipeline (1 then 2 then 3 then 4)

     LOOK BEFORE YOU SPEND
       6.  Preview new leads    (costs a search, writes nothing)
       7.  Preview my messages  (writes nothing, drafts nothing)

     HOUSEKEEPING
       8.  Check everything works
       9.  Open my Sheet
       0.  Quit

MENU
    read -rp "   Type a number and press Enter: " choice
    echo
    case "$choice" in
        1) $OUTREACH run discovery ;;
        2) echo "  Costs about 6 cents per lead."; echo; $OUTREACH run research ;;
        3) $OUTREACH run site-brief ;;
        4) echo "  Nothing gets sent -- drafts land in your Gmail."; echo
           $OUTREACH run outreach-draft ;;
        5) $OUTREACH run discovery && $OUTREACH run research \
             && $OUTREACH run site-brief && $OUTREACH run outreach-draft ;;
        6) $OUTREACH run discovery --dry-run ;;
        7) $OUTREACH run outreach-draft --dry-run ;;
        8) $OUTREACH preflight ;;
        9) sheet_id=$(grep '^SHEET_ID=' .env | cut -d= -f2-)
           [ -n "$sheet_id" ] && open "https://docs.google.com/spreadsheets/d/$sheet_id/edit" \
             2>/dev/null || xdg-open "https://docs.google.com/spreadsheets/d/$sheet_id/edit" 2>/dev/null
           continue ;;
        0) exit 0 ;;
        *) continue ;;
    esac
    pause
done
