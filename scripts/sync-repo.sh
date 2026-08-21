#!/usr/bin/env bash
# Sincroniza este repo com o remoto, chamado pelos hooks SessionStart/Stop do
# Claude Code (ver .claude/settings.json). Dois modos: `pull` (entrada) e
# `push` (saida). Nunca forca (sem --force/-hard/rebase destrutivo) e nunca
# resolve conflito de conteudo sozinho -- so avisa e para.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR" || exit 0

MODE="${1:-pull}"
LOG_PREFIX="[sync-repo]"
STATE_DIR="$REPO_DIR/.claude"
PUSH_DEBOUNCE_FILE="$STATE_DIR/.sync-repo-last-push"
PUSH_DEBOUNCE_SECONDS=180

# Nao roda fora de um repo git, ou se origin nao existir.
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0
git remote get-url origin >/dev/null 2>&1 || exit 0

has_conflict_markers() {
  git diff --check 2>/dev/null | grep -q "conflict marker" && return 0
  grep -rlIE "^(<<<<<<<|=======$|>>>>>>>)" --exclude-dir=.git . 2>/dev/null | grep -q . && return 0
  return 1
}

warn_and_stop() {
  echo "$LOG_PREFIX ATENCAO: $1"
  echo "$LOG_PREFIX Nada foi resolvido automaticamente -- revise o repo (git status) antes de continuar o trabalho."
}

do_pull() {
  # Rede pode estar fora do ar (offline) -- isso nao pode travar a sessao.
  if ! git fetch origin --quiet 2>/tmp/sync-repo-fetch.err; then
    echo "$LOG_PREFIX fetch falhou (rede/credencial?), seguindo com o estado local. Detalhe:"
    tail -n 3 /tmp/sync-repo-fetch.err 2>/dev/null
    return 0
  fi

  local branch behind
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
  [ "$branch" = "HEAD" ] && { echo "$LOG_PREFIX HEAD destacado, pulando auto-sync."; return 0; }
  git rev-parse --verify "origin/$branch" >/dev/null 2>&1 || { echo "$LOG_PREFIX sem origin/$branch, pulando."; return 0; }

  behind="$(git rev-list --count "HEAD..origin/$branch" 2>/dev/null || echo 0)"
  if [ "$behind" = "0" ]; then
    echo "$LOG_PREFIX repo ja atualizado com origin/$branch."
    return 0
  fi

  echo "$LOG_PREFIX $behind commit(s) novo(s) em origin/$branch, sincronizando..."

  local dirty=0
  [ -n "$(git status --porcelain)" ] && dirty=1

  if [ "$dirty" = "1" ]; then
    if ! git stash push -u -q -m "auto-sync $(date -Iseconds)"; then
      warn_and_stop "nao consegui guardar mudancas locais em stash, abortando pull automatico."
      return 1
    fi
  fi

  if ! git merge --ff-only "origin/$branch" -q; then
    warn_and_stop "fast-forward falhou (branch local divergiu do remoto de um jeito nao trivial)."
    [ "$dirty" = "1" ] && git stash pop -q 2>/dev/null
    return 1
  fi

  if [ "$dirty" = "1" ]; then
    local pop_out
    pop_out="$(git stash pop 2>&1)"
    if echo "$pop_out" | grep -q "CONFLICT" || has_conflict_markers; then
      warn_and_stop "conflito de conteudo ao reaplicar suas mudancas locais sobre o que veio do remoto. Arquivos com marcador <<<<<<< precisam de revisao manual (git status mostra quais)."
      return 1
    fi
  fi

  echo "$LOG_PREFIX pull concluido, HEAD agora em $(git rev-parse --short HEAD)."
  return 0
}

do_push() {
  # Debounce: nao adianta tentar empurrar a cada resposta do turno.
  if [ -f "$PUSH_DEBOUNCE_FILE" ]; then
    local last now
    last="$(cat "$PUSH_DEBOUNCE_FILE" 2>/dev/null || echo 0)"
    now="$(date +%s)"
    if [ $(( now - last )) -lt "$PUSH_DEBOUNCE_SECONDS" ]; then
      return 0
    fi
  fi

  # Nunca comita em cima de um merge/conflito ja pendente.
  if [ -f "$REPO_DIR/.git/MERGE_HEAD" ] || has_conflict_markers; then
    echo "$LOG_PREFIX ha conflito/merge pendente, nao vou comitar por cima. Resolva manualmente primeiro."
    return 1
  fi

  local branch
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
  [ "$branch" = "HEAD" ] && return 0

  # So mexe em diretorios de artefato conhecido: memoria de agente, agentes,
  # skills, saidas geradas e documentacao. Nunca 'git add -A' -- arquivo solto
  # na raiz (ex.: captura de rede, dump temporario) fica de fora sempre.
  local allow_paths=(
    ".claude/agent-memory"
    ".claude/agents"
    ".claude/skills"
    "output"
    "docs"
    "README.md"
  )
  local staged_any=0
  for p in "${allow_paths[@]}"; do
    [ -e "$p" ] || continue
    if [ -n "$(git status --porcelain -- "$p")" ]; then
      git add -- "$p"
      staged_any=1
    fi
  done

  if [ "$staged_any" = "0" ]; then
    return 0
  fi

  local changed
  changed="$(git diff --cached --name-only | sed 's/^/  - /')"
  local host
  host="$(hostname 2>/dev/null || echo maquina-desconhecida)"

  if ! git commit -q -m "Auto-sync (${host}): atualiza memoria/output/docs

${changed}

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"; then
    echo "$LOG_PREFIX nada para comitar (mudancas eram so em arquivos fora do allowlist)."
    return 0
  fi

  # Antes de empurrar, tenta um fast-forward do que tiver de novo no remoto
  # (sem rebase, sem force) -- se nao der de forma trivial, para e avisa.
  git fetch origin --quiet 2>/dev/null || true
  if ! git merge --ff-only "origin/$branch" -q 2>/dev/null; then
    warn_and_stop "commit local criado mas origin/$branch tem historico que nao da pra integrar automaticamente (fast-forward falhou). O commit fica local, NAO empurrado -- resolva manualmente (git pull/merge) e depois 'git push'."
    return 1
  fi

  if ! git push origin "$branch" -q; then
    echo "$LOG_PREFIX push falhou (rede/credencial/permissao?). Commit fica local, tentarei de novo na proxima sincronizacao."
    return 1
  fi

  date +%s > "$PUSH_DEBOUNCE_FILE"
  echo "$LOG_PREFIX enviado para origin/$branch: $(git rev-parse --short HEAD)."
  return 0
}

case "$MODE" in
  pull) do_pull ;;
  push) do_push ;;
  *) echo "$LOG_PREFIX modo desconhecido: $MODE (use 'pull' ou 'push')"; exit 1 ;;
esac
