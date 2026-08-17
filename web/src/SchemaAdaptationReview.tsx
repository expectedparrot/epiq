import { useMemo, useState } from "react";

import { ResearchJob } from "./api";

export function SchemaAdaptationReviews({
  jobs,
  onAccept,
}: {
  jobs: ResearchJob[];
  onAccept: (questionId: string) => Promise<void>;
}) {
  const groups = useMemo(() => {
    const grouped = new Map<
      string,
      { label: string; jobs: number; findings: number; applying: boolean }
    >();
    for (const job of jobs) {
      const proposal = job.schema_adaptation;
      if (!proposal || !["pending", "applying"].includes(proposal.status)) continue;
      const current = grouped.get(proposal.question_id) ?? {
        label: proposal.label,
        jobs: 0,
        findings: 0,
        applying: false,
      };
      current.jobs += 1;
      current.findings +=
        job.relationship_suggestions?.filter((item) => item.status === "pending")
          .length ?? 0;
      current.applying ||= proposal.status === "applying";
      grouped.set(proposal.question_id, current);
    }
    return [...grouped.entries()];
  }, [jobs]);

  return groups.map(([questionId, group]) => (
    <SchemaAdaptationReview
      key={questionId}
      questionId={questionId}
      label={group.label}
      jobs={group.jobs}
      findings={group.findings}
      recovering={group.applying}
      onAccept={onAccept}
    />
  ));
}

function SchemaAdaptationReview({
  questionId,
  label,
  jobs,
  findings,
  recovering,
  onAccept,
}: {
  questionId: string;
  label: string;
  jobs: number;
  findings: number;
  recovering: boolean;
  onAccept: (questionId: string) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <article className="activity-job schema-adaptation-review">
      <div className="activity-job-head">
        <span className="job-status completed">review</span>
        <code>{questionId.replace(/^q_|_v\d+$/g, "")}</code>
      </div>
      <b>Agent findings do not fit the field schema</b>
      <p>
        {jobs} researched row{jobs === 1 ? "" : "s"} found multiple {label.toLowerCase()}.
        Change this field from one related row to multiple related rows and add all {findings}{" "}
        staged relationship{findings === 1 ? "" : "s"}?
      </p>
      <small>
        Epiq will atomically create a new field version and preserve every source and finding.
      </small>
      <button
        className="primary add-relationship-proposals"
        disabled={busy || findings === 0}
        onClick={async () => {
          setBusy(true);
          try {
            await onAccept(questionId);
          } finally {
            setBusy(false);
          }
        }}
      >
        {busy
          ? "Applying change set…"
          : recovering
            ? `Recover and apply ${findings} findings`
            : `Approve change + ${findings} findings`}
      </button>
    </article>
  );
}
