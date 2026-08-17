#!/usr/bin/env bash
set -euo pipefail

output_dir="${1:-/tmp/epiq-cli-examples}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$output_dir"

for example in hiring-committee investment-opportunities competitor-features public-figure-writing; do
  "$script_dir/$example/build.sh" "$output_dir/$example.sqlite"
done

echo "Built CLI examples in $output_dir"

