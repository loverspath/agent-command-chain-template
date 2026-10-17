#!/usr/bin/env bash
# ==============================================================================
# tools/ts/install.sh - ts (TMUX 세션 매니저 & 체인 복구/기동 도구) 설치/설정 스크립트
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

TARGET_BIN="$REPO_ROOT/bin/ts"
FALLBACK_BIN="$SCRIPT_DIR/bin/ts"
INSTALL_DEST="$HOME/bin/ts"

# 색상 정의
if [[ -t 1 ]]; then
  GREEN='\033[32m'
  YELLOW='\033[33m'
  CYAN='\033[36m'
  RED='\033[31m'
  BOLD='\033[1m'
  RESET='\033[0m'
else
  GREEN=''
  YELLOW=''
  CYAN=''
  RED=''
  BOLD=''
  RESET=''
fi

usage() {
  echo "사용법: $0 [옵션]"
  echo "옵션:"
  echo "  --help, -h       도움말 출력"
  echo "  --uninstall      ~/bin/ts 심볼릭 링크 제거"
  echo "  --check          설치 상태 점검"
  exit 0
}

check_status() {
  echo -e "${BOLD}${CYAN}[ts 설치 상태 점검]${RESET}"
  if [[ -L "$INSTALL_DEST" ]]; then
    local link_target
    link_target="$(readlink -f "$INSTALL_DEST" || true)"
    echo -e "  - 심볼릭 링크: ${GREEN}$INSTALL_DEST -> $link_target${RESET}"
    if [[ -x "$INSTALL_DEST" ]]; then
      echo -e "  - 실행 권한: ${GREEN}정상 (실행 가능)${RESET}"
    else
      echo -e "  - 실행 권한: ${RED}비정상 (실행 불가)${RESET}"
    fi
  elif [[ -e "$INSTALL_DEST" ]]; then
    echo -e "  - 심볼릭 링크: ${YELLOW}$INSTALL_DEST (일반 파일로 존재)${RESET}"
  else
    echo -e "  - 심볼릭 링크: ${YELLOW}미설치 (존재하지 않음)${RESET}"
  fi

  if [[ ":$PATH:" == *":$HOME/bin:"* ]]; then
    echo -e "  - PATH 환경변수: ${GREEN}정상 (~/bin 포함됨)${RESET}"
  else
    echo -e "  - PATH 환경변수: ${YELLOW}경고 (~/bin 미포함, ~/.bashrc 확인 필요)${RESET}"
  fi

  if command -v ts >/dev/null 2>&1; then
    local ts_path
    ts_path="$(command -v ts)"
    echo -e "  - 활성 명령어: ${GREEN}$ts_path${RESET}"
  else
    echo -e "  - 활성 명령어: ${YELLOW}찾을 수 없음${RESET}"
  fi
}

uninstall_ts() {
  echo -e "${CYAN}[ts 제거]${RESET}"
  if [[ -L "$INSTALL_DEST" ]]; then
    rm -f "$INSTALL_DEST"
    echo -e "${GREEN}심볼릭 링크 $INSTALL_DEST 가 안전하게 제거되었습니다.${RESET}"
  elif [[ -e "$INSTALL_DEST" ]]; then
    echo -e "${YELLOW}경고: $INSTALL_DEST 가 심볼릭 링크가 아닌 일반 파일입니다. 직접 확인 후 삭제하세요.${RESET}"
  else
    echo -e "${GREEN}제거할 $INSTALL_DEST 파일이 없습니다.${RESET}"
  fi
}

main() {
  case "${1:-}" in
    -h|--help)
      usage
      ;;
    --check)
      check_status
      exit 0
      ;;
    --uninstall)
      uninstall_ts
      exit 0
      ;;
  esac

  echo -e "${BOLD}${CYAN}=== ts (TMUX & 에이전트 체인 관리 도구) 설치 ===${RESET}"

  # 1. 원본 바이너리 위치 결정
  local src_bin=""
  if [[ -x "$TARGET_BIN" ]]; then
    src_bin="$TARGET_BIN"
  elif [[ -x "$FALLBACK_BIN" ]]; then
    src_bin="$FALLBACK_BIN"
  else
    echo -e "${RED}[오류] ts 실행 파일을 찾을 수 없습니다: $TARGET_BIN 또는 $FALLBACK_BIN${RESET}" >&2
    exit 1
  fi

  # 2. 실행 권한 보장
  chmod +x "$REPO_ROOT/bin/ts" "$REPO_ROOT/bin/ts-tui" 2>/dev/null || true
  chmod +x "$SCRIPT_DIR/bin/ts" "$SCRIPT_DIR/bin/ts-tui" 2>/dev/null || true

  # 3. ~/bin 디렉터리 준비
  mkdir -p "$HOME/bin"

  # 4. 심볼릭 링크 생성
  ln -sf "$src_bin" "$INSTALL_DEST"
  echo -e "${GREEN}[성공] 심볼릭 링크 생성 완료: $INSTALL_DEST -> $src_bin${RESET}"

  # 5. PATH 점검
  if [[ ":$PATH:" != *":$HOME/bin:"* ]]; then
    echo -e "\n${YELLOW}[알림] 현재 PATH에 ~/bin 이 포함되어 있지 않습니다.${RESET}"
    echo -e "다음 설정을 ${BOLD}~/.bashrc${RESET} 에 추가하세요:"
    echo -e "  ${CYAN}export PATH=\"\$HOME/bin:\$PATH\"${RESET}"
    echo -e "추가 후: ${BOLD}source ~/.bashrc${RESET}"
  else
    echo -e "${GREEN}[확인] PATH에 ~/bin 이 정상적으로 포함되어 있습니다.${RESET}"
  fi

  # 6. 동작 확인
  echo -e "\n${BOLD}${CYAN}[동작 점검]${RESET}"
  if "$INSTALL_DEST" help >/dev/null 2>&1; then
    echo -e "${GREEN}[성공] 'ts' 도구가 정상적으로 응답합니다.${RESET}"
    echo -e "이제 터미널 어디서든 ${BOLD}ts${RESET} 를 입력하여 실행할 수 있습니다."
  else
    echo -e "${YELLOW}[주의] 'ts help' 실행 중 경고가 발생했습니다. 권한 및 의존성을 확인하세요.${RESET}"
  fi
}

main "$@"
