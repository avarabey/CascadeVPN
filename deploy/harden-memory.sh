#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SLICE_SOURCE="$ROOT/systemd/user-0.slice.d/50-ffknd-memory.conf"
SYSCTL_SOURCE="$ROOT/sysctl/90-ffknd-memory.conf"

STATE_DIR="/var/lib/ffknd-memory"
STATE_FILE="$STATE_DIR/state"
SWAP_FILE="$STATE_DIR/swapfile"
SWAP_SIZE_MIB=1024
SLICE_TARGET="/etc/systemd/system/user-0.slice.d/50-ffknd-memory.conf"
SYSCTL_TARGET="/etc/sysctl.d/90-ffknd-memory.conf"
FSTAB="/etc/fstab"
FSTAB_MARKER="# ffknd-memory-guard-v1"
FSTAB_LINE="$SWAP_FILE none swap sw 0 0 $FSTAB_MARKER"
MANAGED_MARKER="Managed by CascadeVPN REPO 0.2.2"

usage() {
  printf 'Usage: %s {dry-run|apply|status|rollback}\n' "$0" >&2
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_root() {
  [ "$(id -u)" -eq 0 ] || die "this operation must run as root"
}

is_swap_active() {
  swapon --noheadings --show=NAME 2>/dev/null | awk -v path="$SWAP_FILE" '$1 == path { found=1 } END { exit !found }'
}

assert_managed_or_absent() {
  local path="$1"
  if [ -e "$path" ] || [ -L "$path" ]; then
    [ ! -L "$path" ] || die "refusing symlink at $path"
    grep -Fq "$MANAGED_MARKER" "$path" || die "refusing to overwrite unmanaged $path"
  fi
}

atomic_fstab_add() {
  grep -Fqx "$FSTAB_LINE" "$FSTAB" && return 0
  if grep -Fq "$FSTAB_MARKER" "$FSTAB"; then
    die "fstab contains a conflicting $FSTAB_MARKER entry"
  fi

  local tmp
  tmp="$(mktemp "${FSTAB}.ffknd.XXXXXX")"
  trap 'rm -f "${tmp:-}"' RETURN
  cp --preserve=mode,ownership,timestamps "$FSTAB" "$tmp"
  printf '%s\n' "$FSTAB_LINE" >>"$tmp"
  mv -f "$tmp" "$FSTAB"
  trap - RETURN
}

atomic_fstab_remove() {
  local count tmp
  count="$(grep -Fxc "$FSTAB_LINE" "$FSTAB" || true)"
  [ "$count" -le 1 ] || die "fstab contains duplicate managed swap entries"
  [ "$count" -eq 1 ] || return 0

  tmp="$(mktemp "${FSTAB}.ffknd.XXXXXX")"
  trap 'rm -f "${tmp:-}"' RETURN
  awk -v line="$FSTAB_LINE" '$0 != line { print }' "$FSTAB" >"$tmp"
  chown --reference="$FSTAB" "$tmp"
  chmod --reference="$FSTAB" "$tmp"
  touch --reference="$FSTAB" "$tmp"
  mv -f "$tmp" "$FSTAB"
  trap - RETURN
}

current_swappiness() {
  sysctl -n vm.swappiness
}

saved_swappiness() {
  [ -f "$STATE_FILE" ] || die "missing managed state: $STATE_FILE"
  awk -F= '$1 == "previous_swappiness" && $2 ~ /^[0-9]+$/ { print $2; found=1 } END { exit !found }' "$STATE_FILE"
}

show_slice() {
  systemctl show user-0.slice \
    -p MemoryHigh -p MemoryMax -p MemorySwapMax -p TasksMax --no-pager
}

status() {
  local failed=0

  if is_swap_active; then
    printf 'swap: active (%s)\n' "$SWAP_FILE"
  else
    printf 'swap: NOT active (%s)\n' "$SWAP_FILE"
    failed=1
  fi

  if [ "$(grep -Fxc "$FSTAB_LINE" "$FSTAB" || true)" -eq 1 ]; then
    printf 'fstab: managed entry present once\n'
  else
    printf 'fstab: managed entry missing or duplicated\n'
    failed=1
  fi

  for path in "$SLICE_TARGET" "$SYSCTL_TARGET" "$STATE_FILE"; do
    if [ -f "$path" ]; then
      printf 'file: OK %s\n' "$path"
    else
      printf 'file: MISSING %s\n' "$path"
      failed=1
    fi
  done

  printf 'vm.swappiness=%s\n' "$(current_swappiness)"
  [ "$(current_swappiness)" = 10 ] || failed=1
  show_slice

  [ "$(systemctl show user-0.slice -p MemoryHigh --value)" = 402653184 ] || failed=1
  [ "$(systemctl show user-0.slice -p MemoryMax --value)" = 536870912 ] || failed=1
  [ "$(systemctl show user-0.slice -p MemorySwapMax --value)" = 536870912 ] || failed=1
  [ "$(systemctl show user-0.slice -p TasksMax --value)" = 256 ] || failed=1

  [ "$failed" -eq 0 ] || return 1
  printf '%s\n' 'memory guard: OK'
}

dry_run() {
  printf '%s\n' 'No changes will be made.'
  printf 'swap plan: %s MiB at %s\n' "$SWAP_SIZE_MIB" "$SWAP_FILE"
  printf 'current vm.swappiness=%s; target=10\n' "$(current_swappiness)"
  printf '%s\n' 'target user-0.slice: MemoryHigh=384M MemoryMax=512M MemorySwapMax=512M TasksMax=256'
  printf 'free filesystem MiB: %s\n' "$(df -Pm "$STATE_DIR" 2>/dev/null | awk 'NR==2 {print $4}' || df -Pm /var/lib | awk 'NR==2 {print $4}')"
  if [ -e "$SWAP_FILE" ] && [ ! -f "$STATE_FILE" ]; then
    die "$SWAP_FILE exists without managed state"
  fi
  assert_managed_or_absent "$SLICE_TARGET"
  assert_managed_or_absent "$SYSCTL_TARGET"
}

apply_guard() {
  require_root
  command -v flock >/dev/null || die "flock is required"
  command -v swapon >/dev/null || die "swapon is required"
  [ -f "$SLICE_SOURCE" ] || die "missing source $SLICE_SOURCE"
  [ -f "$SYSCTL_SOURCE" ] || die "missing source $SYSCTL_SOURCE"
  grep -Fq "$MANAGED_MARKER" "$SLICE_SOURCE" || die "slice source has no managed marker"
  grep -Fq "$MANAGED_MARKER" "$SYSCTL_SOURCE" || die "sysctl source has no managed marker"
  grep -qw memory /sys/fs/cgroup/cgroup.controllers || die "cgroup v2 memory controller is unavailable"

  exec 9>/run/lock/ffknd-memory.lock
  flock -x 9

  [ ! -L "$STATE_DIR" ] || die "refusing symlink at $STATE_DIR"
  assert_managed_or_absent "$SLICE_TARGET"
  assert_managed_or_absent "$SYSCTL_TARGET"
  if [ -e "$SWAP_FILE" ] && [ ! -f "$STATE_FILE" ]; then
    die "$SWAP_FILE exists without managed state"
  fi

  install -d -m 0700 -o root -g root "$STATE_DIR"
  if [ ! -f "$STATE_FILE" ]; then
    printf 'previous_swappiness=%s\n' "$(current_swappiness)" >"$STATE_FILE"
    chmod 0600 "$STATE_FILE"
  fi

  if [ ! -e "$SWAP_FILE" ]; then
    local available_mib
    available_mib="$(df -Pm "$STATE_DIR" | awk 'NR==2 {print $4}')"
    [ "$available_mib" -ge 1280 ] || die "at least 1280 MiB free space is required"
    if ! fallocate -l "${SWAP_SIZE_MIB}M" "$SWAP_FILE"; then
      dd if=/dev/zero of="$SWAP_FILE" bs=1M count="$SWAP_SIZE_MIB" status=none
    fi
    chmod 0600 "$SWAP_FILE"
    mkswap "$SWAP_FILE" >/dev/null
  fi

  atomic_fstab_add
  is_swap_active || swapon "$SWAP_FILE"
  install -D -m 0644 -o root -g root "$SLICE_SOURCE" "$SLICE_TARGET"
  install -D -m 0644 -o root -g root "$SYSCTL_SOURCE" "$SYSCTL_TARGET"
  sysctl -p "$SYSCTL_TARGET" >/dev/null
  systemctl daemon-reload
  systemctl set-property --runtime user-0.slice \
    MemoryHigh=384M MemoryMax=512M MemorySwapMax=512M TasksMax=256
  status
}

rollback_guard() {
  require_root
  command -v flock >/dev/null || die "flock is required"
  exec 9>/run/lock/ffknd-memory.lock
  flock -x 9

  [ -f "$STATE_FILE" ] || die "nothing to roll back: managed state is absent"
  assert_managed_or_absent "$SLICE_TARGET"
  assert_managed_or_absent "$SYSCTL_TARGET"

  local previous
  previous="$(saved_swappiness)"
  atomic_fstab_remove
  is_swap_active && swapoff "$SWAP_FILE"
  rm -f "$SLICE_TARGET" "$SYSCTL_TARGET" "$SWAP_FILE"
  systemctl daemon-reload
  systemctl set-property --runtime user-0.slice \
    MemoryHigh=infinity MemoryMax=infinity MemorySwapMax=infinity TasksMax=infinity
  sysctl -w "vm.swappiness=$previous" >/dev/null
  rm -f "$STATE_FILE"
  rmdir "$STATE_DIR" 2>/dev/null || true
  printf '%s\n' 'memory guard rolled back'
}

case "${1:-}" in
  dry-run) dry_run ;;
  apply) apply_guard ;;
  status) status ;;
  rollback) rollback_guard ;;
  *) usage; exit 2 ;;
esac
