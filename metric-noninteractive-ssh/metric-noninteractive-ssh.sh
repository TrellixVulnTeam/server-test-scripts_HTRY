#!/bin/sh

set -eux

# ISO-8601 time format with final Z for the UTC designator.
# See: https://en.wikipedia.org/wiki/ISO_8601#Coordinated_Universal_Time_(UTC)
# InfluxDB likes this format.
timestamp=$(date --utc '+%Y-%m-%dT%H:%M:%SZ')

WHAT=${WHAT-container}
CPU=${CPU-1}
MEM=${MEM-1}
INSTTYPE="c$CPU-m$MEM"
RELEASE=${RELEASE-$(distro-info --devel)}
INSTNAME=${INSTNAME-metric-ssh-$RELEASE-$WHAT-$INSTTYPE}

cleanup() {
  if lxc info "$INSTNAME" >/dev/null 2>&1; then
    echo "Cleaning up: $INSTNAME"
    lxc delete "$INSTNAME" --force
  fi
}

trap cleanup EXIT

setup_lxd_minimal_remote() {
  # Minimal images are leaner and boot faster.
  lxc remote list --format csv | grep -q '^ubuntu-minimal-daily,' && return
  lxc remote add --protocol simplestreams ubuntu-minimal-daily https://cloud-images.ubuntu.com/minimal/daily/
}

cexec() {
  # This assumes that in the official LXD images
  # user 'ubuntu' always has UID 1000.
  lxc exec --user=1000 --cwd=/home/ubuntu "$INSTNAME" -- "$@"
}

Cexec() {
  # capital C => root
  lxc exec "$INSTNAME" -- "$@"
}

setup_container() {
  [ "$WHAT" = vm ] && vmflag=--vm || vmflag=""
  # shellcheck disable=SC2086
  lxc launch "ubuntu-minimal-daily:$RELEASE" "$INSTNAME" --ephemeral $vmflag

  # Wait for instance to be able to accept commands
  retry -d 2 -t 90 lxc exec "$INSTNAME" true

  # Wait for cloud-init to finish
  # Run as root as the ubuntu (uid 1000) user may not be ready yet.
  Cexec cloud-init status --wait >/dev/null

  # We'll use hyperfine to run the measurement
  Cexec apt-get -q update
  Cexec apt-get -qy install hyperfine

  # Setup passwordless ssh authentication
  cexec ssh-keygen -q -t rsa -f /home/ubuntu/.ssh/id_rsa -N ''
  cexec cp /home/ubuntu/.ssh/id_rsa.pub /home/ubuntu/.ssh/authorized_keys
}

wait_load_settled() {
  # Wait until load is load is settled
  load_settled=false
  for _ in $(seq 1 60); do
    loadavg1=$(cexec cut -d ' ' -f 1 /proc/loadavg)
    loadavg5=$(cexec cut -d ' ' -f 2 /proc/loadavg)
    loadreldiff=$(echo "($loadavg1-$loadavg5)/$loadavg5" | bc -l)
    absloadreldirr=$(echo "if ($loadreldiff < 0) {-($loadreldiff)} else {$loadreldiff}" | bc -l)
    if [ "$(echo "$absloadreldirr < 0.07" | bc -l)" = 1 ]; then
      load_settled=true
      break
    fi
    sleep 10
  done

  if [ $load_settled != true ]; then
    echo "WARNING: load didn't settle!"
  fi
}

do_measurement() {
  # Measure the very first ssh login time.
  # The hyperfine version in Jammy requires at least two runs.
  # Not a problem: we'll keep only the first one when parsing the measurement.
  cexec hyperfine --style=basic --runs=2 --export-json=results-first.json \
    "ssh -o StrictHostKeyChecking=accept-new localhost true"

  # Repeated mesasurement
  cexec hyperfine --style=basic --warmup 10 --runs=50 --export-json=results-warm.json \
    "ssh -o StrictHostKeyChecking=accept-new localhost true"

  # Retrieve measurement results
  lxc file pull "$INSTNAME/home/ubuntu/results-first.json" "results-$RELEASE-$WHAT-c$CPU-m$MEM-$timestamp-first.json"
  lxc file pull "$INSTNAME/home/ubuntu/results-warm.json" "results-$RELEASE-$WHAT-c$CPU-m$MEM-$timestamp-warm.json"
}

cleanup
setup_lxd_minimal_remote
setup_container
wait_load_settled
do_measurement
cleanup
