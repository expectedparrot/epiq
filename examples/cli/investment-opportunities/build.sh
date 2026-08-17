#!/usr/bin/env bash
set -euo pipefail

database="${1:-/tmp/epiq-investments.sqlite}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
epiq_bin="${EPIQ_BIN:-epiq}"

if [[ -e "$database" ]]; then
  echo "Refusing to replace existing database: $database" >&2
  exit 2
fi

"$epiq_bin" --db "$database" init --name "Synthetic Investment Pipeline"
"$epiq_bin" --db "$database" entity Investor "Northstar Ventures"
"$epiq_bin" --db "$database" entity Investor "Harbor Capital"
"$epiq_bin" --db "$database" entity Company "Aster Labs"
"$epiq_bin" --db "$database" entity Company "Beacon Robotics"
"$epiq_bin" --db "$database" entity Company "Cedar Health"

"$epiq_bin" --db "$database" question stage --for Company \
  --type 'Enum[pre_seed,seed,series_a,growth,public]' --definition '{"label":"Financing stage"}'
"$epiq_bin" --db "$database" question amount_raised --for Company --type 'Quantity[USD]' \
  --definition '{"label":"Total amount raised","volatility":"dynamic","freshness_days":90}'
"$epiq_bin" --db "$database" question lead_investor --for Company --type 'Ref[Investor]' \
  --definition '{"label":"Lead investor"}'
"$epiq_bin" --db "$database" question investment_probability --for Company --type Probability \
  --definition '{"label":"Internal probability of investment","volatility":"dynamic","freshness_days":30}'
"$epiq_bin" --db "$database" question key_risks --for Company --type String \
  --definition '{"label":"Key risks","cardinality":"many"}'

"$epiq_bin" --db "$database" --actor agent:example batch-write --input "$script_dir/writeback.json"
"$epiq_bin" --db "$database" doctor

echo "Built $database"

