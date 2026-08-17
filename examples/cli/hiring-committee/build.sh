#!/usr/bin/env bash
set -euo pipefail

database="${1:-/tmp/epiq-hiring.sqlite}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
epiq_bin="${EPIQ_BIN:-epiq}"

if [[ -e "$database" ]]; then
  echo "Refusing to replace existing database: $database" >&2
  exit 2
fi

"$epiq_bin" --db "$database" init --name "Synthetic Hiring Committee"
"$epiq_bin" --db "$database" entity Role "Applied Scientist"
"$epiq_bin" --db "$database" entity Role "Research Engineer"
"$epiq_bin" --db "$database" entity Candidate "Alex Rivera"
"$epiq_bin" --db "$database" entity Candidate "Morgan Lee"

"$epiq_bin" --db "$database" question current_role --for Candidate --type String \
  --definition '{"label":"Current role","volatility":"slow","freshness_days":365}'
"$epiq_bin" --db "$database" question technical_strength --for Candidate \
  --type 'Enum[weak,mixed,strong,exceptional]' \
  --definition '{"label":"Technical strength","cardinality":"many"}'
"$epiq_bin" --db "$database" question interviewer_rating --for Candidate --type Probability \
  --definition '{"label":"Interviewer confidence in hire","cardinality":"many"}'
"$epiq_bin" --db "$database" question recommended_role --for Candidate --type 'Ref[Role]' \
  --definition '{"label":"Recommended role","cardinality":"many"}'
"$epiq_bin" --db "$database" question committee_recommendation --for Candidate \
  --type 'Enum[no_hire,lean_no,lean_yes,hire]' \
  --definition '{"label":"Committee recommendation"}'

"$epiq_bin" --db "$database" --actor agent:example batch-write --input "$script_dir/writeback.json"
"$epiq_bin" --db "$database" doctor

echo "Built $database"

