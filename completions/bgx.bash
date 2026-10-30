# Bash 3.2+; source this file or install as bash-completion/completions/bgx.
_bgx_complete() {
  local IFS=$' \t\n'
  local cur=${COMP_WORDS[COMP_CWORD]} cmd=${COMP_WORDS[1]} word name rest candidate
  local i count=0 tail_value=0 has_tail=0 has_follow=0 has_force=0 has_dry_run=0
  local selected=' ' want_names=0 header=1 has_all=0
  local subcommands='start s stop rename list ls info log remove rm prune help version -h --help -V --version'
  local -a candidates
  COMPREPLY=()
  candidates=()

  for ((i = 1; i < COMP_CWORD; i++)); do
    [[ ${COMP_WORDS[i]} == -- ]] && return 0
  done
  if ((COMP_CWORD == 1)); then
    candidates=($subcommands)
  elif [[ $cmd == help || $cmd == -h || $cmd == --help ]]; then
    ((COMP_CWORD == 2)) && candidates=($subcommands)
  else
    for ((i = 2; i < COMP_CWORD; i++)); do
      word=${COMP_WORDS[i]}
      if ((tail_value)); then tail_value=0; continue; fi
      case $word in
        -h|--help) return 0 ;;
        --all) has_all=1 ;;
        --tail) has_tail=1; tail_value=1 ;;
        -f|--follow) has_follow=1 ;;
        --force) has_force=1 ;;
        --dry-run) has_dry_run=1 ;;
        -*) return 0 ;;
        *) count=$((count + 1)); selected="$selected$word " ;;
      esac
    done
    ((tail_value)) && return 0
    case $cmd in
      start|s) ((count <= 1)) && candidates=(-- -h --help) ;;
      stop)
        candidates=(-h --help)
        if ((!has_all)); then
          want_names=1
          ((count)) || candidates+=(--all)
        fi
        ;;
      remove|rm)
        want_names=1; candidates=(-h --help)
        ((has_force)) || candidates+=(--force)
        ;;
      rename|info|log)
        ((count == 0)) && want_names=1
        candidates=(-h --help)
        if [[ $cmd == log ]]; then
          ((has_tail)) || candidates+=(--tail)
          ((has_follow)) || candidates+=(-f --follow)
        fi
        ;;
      prune)
        candidates=(-h --help)
        ((has_dry_run)) || candidates+=(--dry-run)
        ;;
      list|ls|version|-V|--version) candidates=(-h --help) ;;
    esac
    if ((want_names)) && [[ $cur != -* ]]; then
      # Names contain no whitespace. Keep rows even when list reports a damaged
      # record and exits nonzero; stderr must not interrupt the prompt.
      while read -r name rest; do
        if ((header)); then header=0; continue; fi
        [[ -n $name && $selected != *" $name "* ]] && candidates+=("$name")
      done < <(command "${COMP_WORDS[0]}" list 2>/dev/null)
    fi
  fi
  for candidate in "${candidates[@]}"; do
    [[ $candidate == "$cur"* ]] && COMPREPLY+=("$candidate")
  done
  return 0
}

complete -F _bgx_complete bgx
