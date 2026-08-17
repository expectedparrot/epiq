import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  ApiError,
  Cell,
  Matrix,
  Overview,
  ProjectInfo,
  QuestionChallenge,
  Question,
  ResearchJob,
  api,
  post,
} from "./api";

type Selection = {
  entityId: string;
  entityName: string;
  question: Question;
  cell: Cell;
};
type SortState = {
  key: string;
  direction: "asc" | "desc";
};
type Dialog =
  | "entity"
  | "question"
  | "claim"
  | "notFound"
  | "research"
  | "rowResearch"
  | "suggestEntities"
  | "suggestFields"
  | "policy"
  | "challenge"
  | "schemaChallenge"
  | "retireQuestion"
  | "researchChallenge"
  | null;

const today = () => new Date().toISOString().slice(0, 10);
const integerFormat = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 0,
});
const display = (value: unknown) => {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number" && Number.isInteger(value))
    return integerFormat.format(value);
  return String(value);
};
const cellDisplay = (cell: Cell) => {
  if (cell.state === "Answered") return display(cell.value ?? cell.values);
  if (cell.state === "Contested")
    return `${cell.values.length} competing answers`;
  if (cell.state === "NotFound") return "No evidence found";
  return "";
};

function parseValue(raw: string, type: string): unknown {
  if (type === "String" || type.startsWith("Enum[")) return raw;
  if (type === "Int") return Number.parseInt(raw, 10);
  if (type === "Float" || type === "Probability") return Number(raw);
  if (type === "Bool") return raw.toLowerCase() === "true";
  return JSON.parse(raw);
}

export default function App() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [matrix, setMatrix] = useState<Matrix | null>(null);
  const [kind, setKind] = useState("");
  const [selection, setSelection] = useState<Selection | null>(null);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [needsInit, setNeedsInit] = useState(false);
  const [jobs, setJobs] = useState<ResearchJob[]>([]);
  const [researchQuestion, setResearchQuestion] = useState<Question | null>(
    null,
  );
  const [researchMode, setResearchMode] = useState<
    "fill_missing" | "add_evidence"
  >("fill_missing");
  const [researchTarget, setResearchTarget] = useState<{
    entityId: string;
    entityName: string;
  } | null>(null);
  const [rowResearchTarget, setRowResearchTarget] = useState<{
    entityId: string;
    entityName: string;
    missing: number;
  } | null>(null);
  const [showActivity, setShowActivity] = useState(false);
  const [challengedClaimId, setChallengedClaimId] = useState<string | null>(
    null,
  );
  const [projectClosed, setProjectClosed] = useState(false);
  const [showProjects, setShowProjects] = useState(false);
  const [wrapText, setWrapText] = useState(true);
  const [columnOrder, setColumnOrder] = useState<string[]>([]);
  const [columnWidths, setColumnWidths] = useState<Record<string, number>>({});
  const [draggedColumn, setDraggedColumn] = useState<string | null>(null);
  const [schemaChallengeQuestion, setSchemaChallengeQuestion] =
    useState<Question | null>(null);
  const [retireQuestion, setRetireQuestion] = useState<Question | null>(null);
  const [questionChallenges, setQuestionChallenges] = useState<
    QuestionChallenge[]
  >([]);
  const [showSchemaReview, setShowSchemaReview] = useState(false);
  const [filterText, setFilterText] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [sort, setSort] = useState<SortState>({
    key: "__entity__",
    direction: "asc",
  });
  const [activeGridCell, setActiveGridCell] = useState<{
    entityId: string;
    questionId: string;
  } | null>(null);
  const [clipboardNotice, setClipboardNotice] = useState("");
  const layoutKey = `epiq-layout:${overview?.project.project_id ?? "project"}:${kind}`;

  const loadOverview = useCallback(async () => {
    try {
      const next = await api<Overview>("/api/project");
      setOverview(next);
      setNeedsInit(false);
      const primaryKind = [...next.entity_kinds].sort(
        (left, right) =>
          right.questions - left.questions || right.entities - left.entities,
      )[0]?.kind;
      setKind((current) => current || primaryKind || "");
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === "project_not_found")
        setNeedsInit(true);
      else if (
        caught instanceof ApiError &&
        caught.code === "no_active_project"
      )
        setProjectClosed(true);
      else
        setError(
          caught instanceof Error ? caught.message : "Could not load project",
        );
    } finally {
      setLoading(false);
    }
  }, []);

  const loadMatrix = useCallback(async () => {
    if (!kind || needsInit) return;
    try {
      setMatrix(await api<Matrix>(`/api/matrix/${encodeURIComponent(kind)}`));
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not load table",
      );
    }
  }, [kind, needsInit]);
  const loadQuestionChallenges = useCallback(async () => {
    if (needsInit || projectClosed) return;
    try {
      setQuestionChallenges(
        await api<QuestionChallenge[]>("/api/question-challenges?status=open"),
      );
    } catch {
      /* Challenges are supplementary to the matrix projection. */
    }
  }, [needsInit, projectClosed]);

  useEffect(() => {
    void loadOverview();
  }, [loadOverview]);
  useEffect(() => {
    void loadMatrix();
  }, [loadMatrix]);
  useEffect(() => {
    void loadQuestionChallenges();
  }, [loadQuestionChallenges, kind]);
  useEffect(() => {
    if (!kind || !overview) return;
    try {
      const saved = JSON.parse(localStorage.getItem(layoutKey) ?? "{}");
      setWrapText(saved.wrapText ?? true);
      setColumnOrder(Array.isArray(saved.columnOrder) ? saved.columnOrder : []);
      setColumnWidths(saved.columnWidths ?? {});
      setSort(
        saved.sort?.key && ["asc", "desc"].includes(saved.sort.direction)
          ? saved.sort
          : { key: "__entity__", direction: "asc" },
      );
      setStatusFilter(
        ["all", "answered", "unanswered", "review"].includes(saved.statusFilter)
          ? saved.statusFilter
          : "all",
      );
    } catch {
      setWrapText(true);
      setColumnOrder([]);
      setColumnWidths({});
      setSort({ key: "__entity__", direction: "asc" });
      setStatusFilter("all");
    }
  }, [kind, overview, layoutKey]);
  useEffect(() => {
    if (!matrix) return;
    setSelection((current) => {
      if (!current) return current;
      const row = matrix.rows.find(
        (item) => item.entity_id === current.entityId,
      );
      const cell = row?.cells[current.question.name];
      const question = matrix.questions.find(
        (item) => item.name === current.question.name,
      );
      return cell
        ? { ...current, cell, question: question ?? current.question }
        : current;
    });
  }, [matrix]);
  useEffect(() => {
    if (needsInit) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const next = await api<ResearchJob[]>("/api/research/jobs");
        if (!cancelled) {
          const hasActive = next.some(
            (job) => job.status === "queued" || job.status === "running",
          );
          setJobs((current) => {
            const hadActive = current.some(
              (job) => job.status === "queued" || job.status === "running",
            );
            // Research jobs persist claims independently. Re-project while work is
            // active so completed cells appear without waiting for sibling jobs.
            if (hasActive || hadActive) void loadMatrix();
            return next;
          });
        }
      } catch {
        /* The table remains usable if job polling is unavailable. */
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 1500);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [needsInit, loadMatrix]);

  const refresh = async () => {
    setSelection(null);
    await loadOverview();
    await loadMatrix();
  };
  const projectReady = async () => {
    setKind("");
    setMatrix(null);
    setOverview(null);
    setProjectClosed(false);
    setShowProjects(false);
    await loadOverview();
  };
  const closeProject = async () => {
    try {
      await post("/api/projects/close", {});
      setOverview(null);
      setMatrix(null);
      setKind("");
      setProjectClosed(true);
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not close project",
      );
    }
  };
  const openResearch = (
    question: Question,
    mode: "fill_missing" | "add_evidence",
    target: { entityId: string; entityName: string } | null = null,
  ) => {
    setResearchQuestion(question);
    setResearchMode(mode);
    setResearchTarget(target);
    setDialog("research");
  };
  const launchResearch = async (
    question: Question,
    mode: "fill_missing" | "add_evidence",
    instructions: string,
    entityIds: string[] | null,
  ) => {
    try {
      const request = {
        entity_kind: kind,
        question: question.question_id,
        mode,
        instructions,
        entity_ids: entityIds,
        scope: entityIds ? "cell" : "column",
      };
      if (entityIds) {
        const job = await post<ResearchJob>("/api/research/jobs", request);
        setJobs((current) => [job, ...current]);
      } else {
        const result = await post<{ jobs: ResearchJob[] }>(
          "/api/research/column",
          request,
        );
        setJobs((current) => [...result.jobs, ...current]);
      }
      setDialog(null);
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not launch research",
      );
    }
  };
  const activeJobs = useMemo(
    () =>
      new Map(
        jobs
          .filter((job) => job.status === "queued" || job.status === "running")
          .map((job) => [job.question_id, job]),
      ),
    [jobs],
  );
  const activeRowEntityIds = useMemo(
    () =>
      new Set(
        jobs
          .filter(
            (job) =>
              (job.status === "queued" || job.status === "running") &&
              job.scope === "row",
          )
          .flatMap((job) => [
            ...job.target_entity_ids,
            ...(job.requested_entity_ids ?? []),
          ]),
      ),
    [jobs],
  );
  const isCellResearching = (entityId: string, questionId: string) =>
    jobs.some(
      (job) =>
        (job.status === "queued" || job.status === "running") &&
        job.scope !== "row" &&
        job.question_id === questionId &&
        [
          ...job.target_entity_ids,
          ...(job.requested_entity_ids ?? []),
        ].includes(entityId),
    );
  const launchTableResearch = async () => {
    try {
      const result = await post<{ jobs: ResearchJob[] }>(
        "/api/research/table",
        {
          entity_kind: kind,
        },
      );
      setJobs((current) => [...result.jobs, ...current]);
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Could not launch table research",
      );
    }
  };
  const launchRowResearch = async (instructions: string) => {
    if (!rowResearchTarget) return;
    try {
      const result = await post<{ jobs: ResearchJob[] }>("/api/research/rows", {
        entity_kind: kind,
        entity_id: rowResearchTarget.entityId,
        instructions,
      });
      setJobs((current) => [...result.jobs, ...current]);
      setDialog(null);
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Could not launch row research",
      );
    }
  };
  const launchSuggestions = async (count: number, instructions: string) => {
    try {
      const job = await post<ResearchJob>("/api/entity-suggestions/jobs", {
        entity_kind: kind,
        count,
        instructions,
      });
      setJobs((current) => [job, ...current]);
      setDialog(null);
      setShowActivity(true);
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not suggest rows",
      );
    }
  };
  const launchFieldSuggestions = async (
    count: number,
    instructions: string,
  ) => {
    try {
      const job = await post<ResearchJob>("/api/field-suggestions/jobs", {
        entity_kind: kind,
        count,
        instructions,
      });
      setJobs((current) => [job, ...current]);
      setDialog(null);
      setShowActivity(true);
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not suggest fields",
      );
    }
  };
  const acceptFieldSuggestions = async (
    jobId: string,
    suggestionIds: string[],
  ) => {
    try {
      await post(`/api/field-suggestions/${jobId}/accept`, {
        suggestion_ids: suggestionIds,
      });
      await loadOverview();
      await loadMatrix();
      setJobs(await api<ResearchJob[]>("/api/research/jobs"));
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not add fields",
      );
    }
  };
  const updateSuggestion = async (
    jobId: string,
    suggestionId: string,
    action: "accept" | "dismiss",
  ) => {
    try {
      if (action === "accept") {
        await post(`/api/entity-suggestions/${jobId}/accept`, {
          suggestion_id: suggestionId,
        });
        await loadOverview();
        await loadMatrix();
      } else {
        await post(
          `/api/entity-suggestions/${jobId}/${suggestionId}/dismiss`,
          {},
        );
      }
      setJobs(await api<ResearchJob[]>("/api/research/jobs"));
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Could not update suggestion",
      );
    }
  };
  const kinds = useMemo(
    () =>
      [...(overview?.entity_kinds ?? [])].sort(
        (left, right) =>
          right.questions - left.questions || right.entities - left.entities,
      ),
    [overview],
  );
  const displayedQuestions = useMemo(() => {
    const questions = matrix?.questions ?? [];
    const position = new Map(columnOrder.map((name, index) => [name, index]));
    return [...questions].sort((left, right) => {
      const leftPosition = position.get(left.name);
      const rightPosition = position.get(right.name);
      if (leftPosition === undefined && rightPosition === undefined) return 0;
      if (leftPosition === undefined) return 1;
      if (rightPosition === undefined) return -1;
      return leftPosition - rightPosition;
    });
  }, [matrix, columnOrder]);
  const tableWidth =
    132 +
    (columnWidths.__entity__ ?? 220) +
    118 +
    displayedQuestions.reduce(
      (total, question) => total + (columnWidths[question.name] ?? 180),
      0,
    ) +
    50;
  const displayedRows = useMemo(() => {
    const rows = [...(matrix?.rows ?? [])];
    const query = filterText.trim().toLocaleLowerCase();
    const filtered = rows.filter((row) => {
      const cells = Object.values(row.cells);
      const matchesText =
        !query ||
        row.name.toLocaleLowerCase().includes(query) ||
        cells.some((cell) =>
          cellDisplay(cell).toLocaleLowerCase().includes(query),
        );
      const matchesStatus =
        statusFilter === "all" ||
        (statusFilter === "answered" &&
          cells.some((cell) => cell.state === "Answered")) ||
        (statusFilter === "unanswered" &&
          cells.some((cell) => cell.state === "Unasked")) ||
        (statusFilter === "review" &&
          cells.some(
            (cell) =>
              cell.state === "Contested" ||
              cell.state === "NotFound" ||
              cell.temporal?.freshness === "stale",
          ));
      return matchesText && matchesStatus;
    });
    const direction = sort.direction === "asc" ? 1 : -1;
    return filtered.sort((left, right) => {
      const leftValue =
        sort.key === "__entity__"
          ? left.name
          : (left.cells[sort.key]?.value ?? left.cells[sort.key]?.values ?? "");
      const rightValue =
        sort.key === "__entity__"
          ? right.name
          : (right.cells[sort.key]?.value ??
            right.cells[sort.key]?.values ??
            "");
      if (typeof leftValue === "number" && typeof rightValue === "number")
        return (leftValue - rightValue) * direction;
      return (
        String(leftValue).localeCompare(String(rightValue), undefined, {
          numeric: true,
          sensitivity: "base",
        }) * direction
      );
    });
  }, [matrix, filterText, statusFilter, sort]);
  const saveLayout = (
    nextWrapText: boolean,
    nextOrder: string[],
    nextWidths: Record<string, number>,
    nextSort: SortState = sort,
    nextStatusFilter: string = statusFilter,
  ) => {
    localStorage.setItem(
      layoutKey,
      JSON.stringify({
        wrapText: nextWrapText,
        columnOrder: nextOrder,
        columnWidths: nextWidths,
        sort: nextSort,
        statusFilter: nextStatusFilter,
      }),
    );
  };
  const toggleSort = (key: string) => {
    const next: SortState = {
      key,
      direction: sort.key === key && sort.direction === "asc" ? "desc" : "asc",
    };
    setSort(next);
    saveLayout(wrapText, columnOrder, columnWidths, next);
  };
  const updateStatusFilter = (value: string) => {
    setStatusFilter(value);
    saveLayout(wrapText, columnOrder, columnWidths, sort, value);
  };
  const toggleRows = () => {
    const next = !wrapText;
    setWrapText(next);
    saveLayout(next, columnOrder, columnWidths);
  };
  const resizeColumn = (key: string, event: React.MouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
    const startX = event.clientX;
    const startWidth = columnWidths[key] ?? (key === "__entity__" ? 220 : 180);
    const move = (moveEvent: MouseEvent) => {
      const width = Math.min(
        800,
        Math.max(120, startWidth + moveEvent.clientX - startX),
      );
      setColumnWidths((current) => ({ ...current, [key]: width }));
    };
    const stop = (upEvent: MouseEvent) => {
      const width = Math.min(
        800,
        Math.max(120, startWidth + upEvent.clientX - startX),
      );
      const next = { ...columnWidths, [key]: width };
      setColumnWidths(next);
      saveLayout(wrapText, columnOrder, next);
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", stop);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", stop);
  };
  const reorderColumn = (target: string) => {
    if (!draggedColumn || draggedColumn === target) return;
    const names = displayedQuestions.map((question) => question.name);
    const from = names.indexOf(draggedColumn);
    const to = names.indexOf(target);
    if (from < 0 || to < 0) return;
    names.splice(to, 0, names.splice(from, 1)[0]);
    setColumnOrder(names);
    saveLayout(wrapText, names, columnWidths);
    setDraggedColumn(null);
  };
  const activateCell = (rowIndex: number, columnIndex: number) => {
    const row = displayedRows[rowIndex];
    const question = displayedQuestions[columnIndex];
    if (!row || !question) return;
    setActiveGridCell({
      entityId: row.entity_id,
      questionId: question.question_id,
    });
    requestAnimationFrame(() => {
      document
        .querySelector<HTMLElement>(
          `[data-grid-row="${row.entity_id}"][data-grid-column="${question.question_id}"]`,
        )
        ?.focus();
    });
  };
  const handleCellKey = async (
    event: React.KeyboardEvent<HTMLTableCellElement>,
    rowIndex: number,
    columnIndex: number,
    cell: Cell,
  ) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "c") {
      event.preventDefault();
      try {
        await navigator.clipboard.writeText(cellDisplay(cell));
        setClipboardNotice("Copied cell value");
        window.setTimeout(() => setClipboardNotice(""), 1600);
      } catch {
        setError("Clipboard access was unavailable");
      }
      return;
    }
    const movement: Record<string, [number, number]> = {
      ArrowUp: [-1, 0],
      ArrowDown: [1, 0],
      ArrowLeft: [0, -1],
      ArrowRight: [0, 1],
      Tab: [0, event.shiftKey ? -1 : 1],
    };
    if (event.key === "Escape") {
      setSelection(null);
      setActiveGridCell(null);
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      const row = displayedRows[rowIndex];
      const question = displayedQuestions[columnIndex];
      if (row && question) {
        setSelection({
          entityId: row.entity_id,
          entityName: row.name,
          question,
          cell,
        });
      }
      return;
    }
    const delta = movement[event.key];
    if (!delta) return;
    event.preventDefault();
    let nextRow = rowIndex + delta[0];
    let nextColumn = columnIndex + delta[1];
    if (event.key === "Tab") {
      if (nextColumn >= displayedQuestions.length) {
        nextColumn = 0;
        nextRow += 1;
      } else if (nextColumn < 0) {
        nextColumn = displayedQuestions.length - 1;
        nextRow -= 1;
      }
    }
    activateCell(
      Math.max(0, Math.min(displayedRows.length - 1, nextRow)),
      Math.max(0, Math.min(displayedQuestions.length - 1, nextColumn)),
    );
  };

  if (loading)
    return (
      <div className="center">
        <span className="spinner" />
        Opening the ledger…
      </div>
    );
  if (projectClosed) return <ProjectBrowser onReady={projectReady} />;
  if (needsInit) return <Welcome onReady={loadOverview} />;
  if (overview && kinds.length === 0)
    return <FirstTable onReady={loadOverview} />;

  return (
    <div className="app-shell">
      <header>
        <div className="brand">
          <span className="mark">E</span>
          <div>
            <b>Epiq</b>
            <small>Evidence-backed workspace</small>
          </div>
        </div>
        <div className="project-title">
          <button onClick={() => setShowProjects(true)}>
            {overview?.project.name ?? "Untitled project"}⌄
          </button>
        </div>
        <div className="header-actions">
          <button className="ghost" onClick={() => setShowActivity(true)}>
            ✦ Activity{" "}
            {jobs.filter(
              (job) => job.status === "queued" || job.status === "running",
            ).length || ""}
          </button>
          <button className="ghost" onClick={() => setShowSchemaReview(true)}>
            ⚠ Schema
            {questionChallenges.length ? ` ${questionChallenges.length}` : ""}
          </button>
          <button className="ghost" onClick={() => void refresh()}>
            ↻ Refresh
          </button>
          <a
            className="button-link"
            href={`/api/export/${encodeURIComponent(kind)}.xlsx`}
            download
          >
            ↓ Excel
          </a>
          <a
            className="button-link"
            href="/api/export/project.sqlite"
            download
            title="Download a transactionally consistent project backup"
          >
            ↓ Backup
          </a>
          <button className="ghost" onClick={() => void closeProject()}>
            Close project
          </button>
        </div>
      </header>
      <div className="workspace">
        <aside className="sidebar">
          <div className="sidebar-label">Sheets · row type</div>
          {kinds.map((item) => (
            <button
              key={item.kind}
              className={
                kind === item.kind ? "table-link active" : "table-link"
              }
              onClick={() => setKind(item.kind)}
            >
              <span className="table-icon">▦</span>
              {item.kind}
              <small>
                {item.entities} × {item.questions}
              </small>
            </button>
          ))}
          <div className="sidebar-foot">
            <span className="pulse" />
            SQLite connected
          </div>
        </aside>
        <main>
          <div className="toolbar">
            <div>
              <h1>{kind}</h1>
              <p>
                {displayedRows.length}
                {displayedRows.length !== (matrix?.rows.length ?? 0)
                  ? ` of ${matrix?.rows.length ?? 0}`
                  : ""}{" "}
                rows · {matrix?.questions.length ?? 0} research fields
              </p>
            </div>
            <div className="actions">
              <button
                onClick={toggleRows}
                title="Toggle wrapped and fixed-height rows"
              >
                {wrapText ? "▤ Fixed rows" : "↵ Wrap text"}
              </button>
              <button onClick={() => setDialog("question")}>
                ＋ Add field
              </button>
              <button onClick={() => setDialog("suggestFields")}>
                ✦ Suggest fields
              </button>
              <button className="primary" onClick={() => setDialog("entity")}>
                ＋ Add {kind.toLowerCase()}
              </button>
            </div>
          </div>
          <div className="view-toolbar" aria-label="Table view controls">
            <label className="table-search">
              <span>⌕</span>
              <input
                type="search"
                value={filterText}
                onChange={(event) => setFilterText(event.target.value)}
                placeholder={`Filter ${kind.toLowerCase()} rows or values…`}
                aria-label="Filter rows"
              />
            </label>
            <select
              value={statusFilter}
              onChange={(event) => updateStatusFilter(event.target.value)}
              aria-label="Filter by research status"
            >
              <option value="all">All rows</option>
              <option value="answered">Has answers</option>
              <option value="unanswered">Has unanswered fields</option>
              <option value="review">Needs review</option>
            </select>
            {(filterText || statusFilter !== "all") && (
              <button
                className="clear-view-button"
                onClick={() => {
                  setFilterText("");
                  updateStatusFilter("all");
                }}
              >
                Clear filters
              </button>
            )}
            <span className="keyboard-hint">
              Arrows move · Enter inspects · ⌘/Ctrl+C copies
            </span>
            {clipboardNotice && (
              <span className="clipboard-notice">✓ {clipboardNotice}</span>
            )}
          </div>
          {error && (
            <div className="error-banner">
              {error}
              <button onClick={() => setError("")}>×</button>
            </div>
          )}
          <div className="grid-wrap">
            <table
              className={`grid ${wrapText ? "wrap-text" : "compact-rows"}`}
              style={{ width: tableWidth }}
            >
              <colgroup>
                <col style={{ width: 132 }} />
                <col style={{ width: columnWidths.__entity__ ?? 220 }} />
                <col style={{ width: 118 }} />
                {displayedQuestions.map((question) => (
                  <col
                    key={question.name}
                    style={{ width: columnWidths[question.name] ?? 180 }}
                  />
                ))}
                <col style={{ width: 50 }} />
              </colgroup>
              <thead>
                <tr className="field-header-row">
                  <th className="row-number">#</th>
                  <th className="name-column entity-column-head">
                    <div className="entity-column-label">
                      <button
                        className="column-sort-button"
                        onClick={() => toggleSort("__entity__")}
                        title={`Sort by ${kind}`}
                      >
                        {kind}
                        {sort.key === "__entity__" && (
                          <i>{sort.direction === "asc" ? "↑" : "↓"}</i>
                        )}
                      </button>
                      <small>Entity · row identity</small>
                    </div>
                    <span
                      className="column-resizer"
                      onMouseDown={(event) => resizeColumn("__entity__", event)}
                    />
                  </th>
                  <th className="row-action-column">
                    <span>Row research</span>
                    <small>Agent action</small>
                  </th>
                  {displayedQuestions.map((question) => {
                    return (
                      <th
                        key={question.question_id}
                        className={`reorderable-column ${question.schema_state === "challenged" ? "column-challenged" : ""}`}
                        draggable
                        onDragStart={(event) => {
                          if (
                            (event.target as HTMLElement).closest(
                              "button,.column-resizer",
                            )
                          ) {
                            event.preventDefault();
                            return;
                          }
                          setDraggedColumn(question.name);
                        }}
                        onDragOver={(event) => event.preventDefault()}
                        onDrop={() => reorderColumn(question.name)}
                        onDragEnd={() => setDraggedColumn(null)}
                      >
                        <button
                          className="column-sort-button"
                          onClick={() => toggleSort(question.name)}
                          title={`Sort by ${String(question.definition.label ?? question.name)}`}
                        >
                          {String(question.definition.label ?? question.name)}
                          {sort.key === question.name && (
                            <i>{sort.direction === "asc" ? "↑" : "↓"}</i>
                          )}
                        </button>
                        <small>
                          {question.value_type}
                          {question.definition.volatility &&
                          question.definition.volatility !== "stable"
                            ? ` · ${question.definition.volatility}`
                            : ""}
                        </small>
                        <span
                          className="column-resizer"
                          onMouseDown={(event) =>
                            resizeColumn(question.name, event)
                          }
                        />
                      </th>
                    );
                  })}
                  <th className="add-column">
                    <button onClick={() => setDialog("question")}>＋</button>
                  </th>
                </tr>
                <tr className="column-action-row">
                  <th className="row-number table-research-corner">
                    <button
                      title="Research every unanswered cell"
                      onClick={() => void launchTableResearch()}
                    >
                      ✦ Research all
                    </button>
                  </th>
                  <th className="name-column entity-action-head">
                    <button
                      className="suggest-entities-button"
                      title={`Find more ${kind.toLowerCase()} rows`}
                      onClick={() => setDialog("suggestEntities")}
                    >
                      ✦ Suggest more {kind.toLowerCase()}s
                    </button>
                  </th>
                  <th className="row-action-column action-row-label">
                    Per row ↓
                  </th>
                  {displayedQuestions.map((question) => {
                    const job = activeJobs.get(question.question_id);
                    return (
                      <th
                        className="field-action-cell"
                        key={question.question_id}
                      >
                        <div className="column-actions">
                          <button
                            className={
                              job ? "agent-button running" : "agent-button"
                            }
                            title={
                              job
                                ? "Research in progress"
                                : "Research this column"
                            }
                            disabled={Boolean(job)}
                            onClick={() =>
                              openResearch(question, "fill_missing")
                            }
                          >
                            {job
                              ? `✦ ${job.completed}/${job.total || "…"}`
                              : "✦ Research"}
                          </button>
                          <button
                            className="policy-button"
                            title="Set field time policy"
                            onClick={() => {
                              setResearchQuestion(question);
                              setDialog("policy");
                            }}
                          >
                            ◷
                          </button>
                          <button
                            className="policy-button schema-button"
                            title="Challenge this field's schema"
                            onClick={() => {
                              setSchemaChallengeQuestion(question);
                              setDialog("schemaChallenge");
                            }}
                          >
                            {question.schema_state === "challenged" ? "⚠" : "?"}
                          </button>
                          <button
                            className="policy-button retire-field-button"
                            title="Remove this field from the table"
                            onClick={() => {
                              setRetireQuestion(question);
                              setDialog("retireQuestion");
                            }}
                          >
                            ×
                          </button>
                        </div>
                      </th>
                    );
                  })}
                  <th className="add-column" />
                </tr>
              </thead>
              <tbody>
                {displayedRows.map((row, index) => {
                  const isResearching = activeRowEntityIds.has(row.entity_id);
                  return (
                    <tr
                      key={row.entity_id}
                      className={isResearching ? "row-is-researching" : ""}
                    >
                      <td className="row-number">
                        {isResearching ? (
                          <span className="row-spinner" />
                        ) : (
                          index + 1
                        )}
                      </td>
                      <td className="entity-name">
                        <div className="entity-inner">
                          <span>{row.name}</span>
                        </div>
                      </td>
                      <td className="row-action-cell">
                        <button
                          className="row-agent-button"
                          title={`Research unanswered fields for ${row.name}`}
                          onClick={() => {
                            setRowResearchTarget({
                              entityId: row.entity_id,
                              entityName: row.name,
                              missing: displayedQuestions.filter(
                                (question) =>
                                  row.cells[question.name].state === "Unasked",
                              ).length,
                            });
                            setDialog("rowResearch");
                          }}
                        >
                          ✦ Research
                        </button>
                      </td>
                      {displayedQuestions.map((question) => {
                        const cell = row.cells[question.name];
                        return (
                          <td
                            key={question.question_id}
                            data-grid-row={row.entity_id}
                            data-grid-column={question.question_id}
                            tabIndex={
                              activeGridCell?.entityId === row.entity_id &&
                              activeGridCell?.questionId ===
                                question.question_id
                                ? 0
                                : -1
                            }
                            className={`data-cell type-${question.value_type.toLowerCase()} state-${cell.state.toLowerCase()} ${isCellResearching(row.entity_id, question.question_id) ? "cell-is-researching" : ""} ${activeGridCell?.entityId === row.entity_id && activeGridCell?.questionId === question.question_id ? "active-cell" : ""}`}
                            onFocus={() =>
                              setActiveGridCell({
                                entityId: row.entity_id,
                                questionId: question.question_id,
                              })
                            }
                            onKeyDown={(event) =>
                              void handleCellKey(
                                event,
                                index,
                                displayedQuestions.indexOf(question),
                                cell,
                              )
                            }
                            onClick={() => {
                              setActiveGridCell({
                                entityId: row.entity_id,
                                questionId: question.question_id,
                              });
                            }}
                            onDoubleClick={() => {
                              setSelection({
                                entityId: row.entity_id,
                                entityName: row.name,
                                question,
                                cell,
                              });
                            }}
                          >
                            <div className="cell-value">
                              {isCellResearching(
                                row.entity_id,
                                question.question_id,
                              ) && <span className="cell-spinner" />}
                              {cellDisplay(cell)}
                            </div>
                            {cell.state !== "Unasked" && (
                              <span className="state-dot" title={cell.state} />
                            )}
                            {cell.lineage.length > 0 && (
                              <span className="source-count">
                                {cell.lineage.length} src
                              </span>
                            )}
                          </td>
                        );
                      })}
                      <td className="add-column" />
                    </tr>
                  );
                })}
                <tr className="add-row">
                  <td />
                  <td colSpan={(matrix?.questions.length ?? 0) + 3}>
                    <button onClick={() => setDialog("entity")}>
                      ＋ Add row
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
            {matrix?.rows.length === 0 && (
              <div className="empty">
                <div>▦</div>
                <h2>Your research table is empty</h2>
                <p>
                  Add a row, then define fields you want agents or people to
                  research.
                </p>
                <button className="primary" onClick={() => setDialog("entity")}>
                  Add first {kind.toLowerCase()}
                </button>
              </div>
            )}
            {(matrix?.rows.length ?? 0) > 0 && displayedRows.length === 0 && (
              <div className="empty filtered-empty">
                <div>⌕</div>
                <h2>No rows match this view</h2>
                <p>Try a different search or clear the status filter.</p>
                <button
                  onClick={() => {
                    setFilterText("");
                    updateStatusFilter("all");
                  }}
                >
                  Clear filters
                </button>
              </div>
            )}
          </div>
        </main>
        {selection && (
          <CellDrawer
            selection={selection}
            isResearching={isCellResearching(
              selection.entityId,
              selection.question.question_id,
            )}
            onClose={() => setSelection(null)}
            onAdd={() => setDialog("claim")}
            onNotFound={() => setDialog("notFound")}
            onEnrich={() =>
              openResearch(selection.question, "add_evidence", {
                entityId: selection.entityId,
                entityName: selection.entityName,
              })
            }
            onResearch={() =>
              openResearch(selection.question, "fill_missing", {
                entityId: selection.entityId,
                entityName: selection.entityName,
              })
            }
            onPolicy={() => setDialog("policy")}
            onSchemaChallenge={() => {
              setSchemaChallengeQuestion(selection.question);
              setDialog("schemaChallenge");
            }}
            onChallengeResearch={() => setDialog("researchChallenge")}
            onChallenge={(claimId) => {
              setChallengedClaimId(claimId);
              setDialog("challenge");
            }}
            onChanged={refresh}
          />
        )}
        {showActivity && (
          <ActivityPanel
            jobs={jobs}
            onClose={() => setShowActivity(false)}
            onSuggestion={updateSuggestion}
            onAcceptFields={acceptFieldSuggestions}
          />
        )}
        {showSchemaReview && (
          <SchemaReviewPanel
            challenges={questionChallenges}
            onClose={() => setShowSchemaReview(false)}
            onChanged={async () => {
              await loadQuestionChallenges();
              await loadMatrix();
            }}
          />
        )}
      </div>
      {dialog === "entity" && (
        <EntityDialog
          kind={kind}
          onClose={() => setDialog(null)}
          onSaved={refresh}
        />
      )}
      {dialog === "question" && (
        <QuestionDialog
          kind={kind}
          onClose={() => setDialog(null)}
          onSaved={refresh}
        />
      )}
      {dialog === "claim" && selection && (
        <ClaimDialog
          selection={selection}
          onClose={() => setDialog(null)}
          onSaved={refresh}
        />
      )}
      {dialog === "notFound" && selection && (
        <NotFoundDialog
          selection={selection}
          onClose={() => setDialog(null)}
          onSaved={refresh}
        />
      )}
      {dialog === "research" && researchQuestion && (
        <ResearchDialog
          kind={kind}
          question={researchQuestion}
          initialMode={researchMode}
          initialTarget={researchTarget}
          onClose={() => setDialog(null)}
          onLaunch={launchResearch}
        />
      )}
      {dialog === "rowResearch" && rowResearchTarget && (
        <RowResearchDialog
          kind={kind}
          target={rowResearchTarget}
          onClose={() => setDialog(null)}
          onLaunch={launchRowResearch}
        />
      )}
      {dialog === "suggestEntities" && (
        <SuggestEntitiesDialog
          kind={kind}
          onClose={() => setDialog(null)}
          onLaunch={launchSuggestions}
        />
      )}
      {dialog === "suggestFields" && (
        <SuggestFieldsDialog
          kind={kind}
          fieldCount={matrix?.questions.length ?? 0}
          onClose={() => setDialog(null)}
          onLaunch={launchFieldSuggestions}
        />
      )}
      {dialog === "policy" && researchQuestion && (
        <PolicyDialog
          question={researchQuestion}
          onClose={() => setDialog(null)}
          onSaved={refresh}
        />
      )}
      {dialog === "challenge" && selection && challengedClaimId && (
        <ChallengeDialog
          kind={kind}
          selection={selection}
          claimId={challengedClaimId}
          onClose={() => setDialog(null)}
          onSaved={async (job) => {
            if (job) setJobs((current) => [job, ...current]);
            setDialog(null);
            await refresh();
          }}
        />
      )}
      {dialog === "schemaChallenge" && schemaChallengeQuestion && (
        <QuestionChallengeDialog
          question={schemaChallengeQuestion}
          rows={matrix?.rows ?? []}
          onClose={() => setDialog(null)}
          onSaved={async () => {
            setDialog(null);
            await loadQuestionChallenges();
            await loadMatrix();
          }}
        />
      )}
      {dialog === "retireQuestion" && retireQuestion && (
        <RetireQuestionDialog
          question={retireQuestion}
          onClose={() => setDialog(null)}
          onSaved={async () => {
            setDialog(null);
            setRetireQuestion(null);
            setSelection(null);
            setActiveGridCell(null);
            await loadOverview();
            await loadMatrix();
          }}
        />
      )}
      {dialog === "researchChallenge" && selection?.cell.research && (
        <ResearchOutcomeChallengeDialog
          kind={kind}
          selection={selection}
          onClose={() => setDialog(null)}
          onSaved={async (job) => {
            setJobs((current) => [job, ...current]);
            setDialog(null);
            await refresh();
          }}
        />
      )}
      {showProjects && (
        <ProjectManager
          onClose={() => setShowProjects(false)}
          onReady={projectReady}
        />
      )}
    </div>
  );
}

function ProjectBrowser({ onReady }: { onReady: () => Promise<void> }) {
  return (
    <div className="welcome">
      <div className="welcome-card project-browser-card">
        <span className="mark large">E</span>
        <div className="eyebrow">EPIQ PROJECTS</div>
        <h1>What would you like to research?</h1>
        <p>Open a local project or start a new evidence-backed workspace.</p>
        <ProjectList onReady={onReady} />
      </div>
    </div>
  );
}

function ProjectManager({
  onClose,
  onReady,
}: {
  onClose: () => void;
  onReady: () => Promise<void>;
}) {
  return (
    <Modal
      title="Projects"
      subtitle="Each project is a portable, independent SQLite database."
      onClose={onClose}
    >
      <ProjectList onReady={onReady} />
    </Modal>
  );
}

function ProjectList({ onReady }: { onReady: () => Promise<void> }) {
  const [projects, setProjects] = useState<ProjectInfo[]>([]);
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    void api<ProjectInfo[]>("/api/projects")
      .then(setProjects)
      .catch((caught) =>
        setError(
          caught instanceof Error ? caught.message : "Could not list projects",
        ),
      );
  }, []);
  const create = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await post("/api/projects", { name });
      await onReady();
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not create project",
      );
    }
  };
  const open = async (projectId: string) => {
    try {
      await post("/api/projects/open", { project_id: projectId });
      await onReady();
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not open project",
      );
    }
  };
  return (
    <div className="project-list">
      <div className="eyebrow">AVAILABLE PROJECTS</div>
      {projects.length === 0 && <p>No saved projects yet.</p>}
      {projects.map((project) => (
        <button
          className="project-option"
          key={project.project_id}
          disabled={project.active}
          onClick={() => void open(project.project_id)}
        >
          <span>
            <b>{project.name}</b>
            <small>{project.path}</small>
          </span>
          <i>{project.active ? "Open now" : "Open →"}</i>
        </button>
      ))}
      <form
        className="new-project-form"
        onSubmit={(event) => void create(event)}
      >
        <label>
          Start a new project
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Project name"
            required
          />
        </label>
        <button className="primary">Create project</button>
      </form>
      {error && <div className="form-error">{error}</div>}
    </div>
  );
}

function Welcome({ onReady }: { onReady: () => Promise<void> }) {
  const [name, setName] = useState("My research workspace");
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await post("/api/project", { name });
      await onReady();
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not initialize",
      );
    }
  };
  return (
    <div className="welcome">
      <div className="welcome-card">
        <span className="mark large">E</span>
        <div className="eyebrow">LOCAL-FIRST RESEARCH</div>
        <h1>
          Build tables that remember
          <br />
          why every answer is there.
        </h1>
        <p>
          Epiq combines a familiar spreadsheet with evidence, confidence,
          history, and competing claims.
        </p>
        <form onSubmit={(event) => void submit(event)}>
          <label>
            Workspace name
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              autoFocus
            />
          </label>
          {error && <div className="form-error">{error}</div>}
          <button className="primary" type="submit">
            Create workspace →
          </button>
        </form>
      </div>
    </div>
  );
}

function FirstTable({ onReady }: { onReady: () => Promise<void> }) {
  const [kind, setKind] = useState("");
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await post("/api/entity-kinds", { kind });
      await onReady();
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Could not create the first sheet",
      );
    }
  };
  return (
    <div className="welcome">
      <div className="welcome-card">
        <span className="mark large">E</span>
        <div className="eyebrow">CREATE YOUR FIRST SHEET</div>
        <h1>
          What does each row
          <br />
          represent?
        </h1>
        <p>
          The row type becomes the identity column. No row is created until you
          explicitly add one.
        </p>
        <form onSubmit={(event) => void submit(event)}>
          <label>
            Row type
            <input
              value={kind}
              onChange={(event) => setKind(event.target.value)}
              placeholder="Investor, Town, Product, Experiment…"
              autoFocus
              required
            />
          </label>
          {error && <div className="form-error">{error}</div>}
          <button className="primary" type="submit">
            Create empty sheet →
          </button>
        </form>
        <small className="onboarding-note">
          Questions you add afterward become research columns.
        </small>
      </div>
    </div>
  );
}

function Modal({
  title,
  subtitle,
  children,
  onClose,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
  onClose: () => void;
}) {
  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <section
        className="modal"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <button className="close" onClick={onClose}>
          ×
        </button>
        <h2>{title}</h2>
        {subtitle && <p>{subtitle}</p>}
        {children}
      </section>
    </div>
  );
}

function EntityDialog({
  kind,
  onClose,
  onSaved,
}: {
  kind: string;
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await post("/api/entities", { kind, name, attributes: {} });
      onClose();
      await onSaved();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not add row");
    }
  };
  return (
    <Modal
      title={`Add ${kind.toLowerCase()}`}
      subtitle="Creates a durable entity—the row itself is never a researched claim."
      onClose={onClose}
    >
      <form onSubmit={(event) => void submit(event)}>
        <label>
          Name
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder={`New ${kind.toLowerCase()} name`}
            autoFocus
            required
          />
        </label>
        {error && <div className="form-error">{error}</div>}
        <div className="modal-actions">
          <button type="button" className="ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="primary">Add row</button>
        </div>
      </form>
    </Modal>
  );
}

function QuestionDialog({
  kind,
  onClose,
  onSaved,
}: {
  kind: string;
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [label, setLabel] = useState("");
  const [type, setType] = useState("String");
  const [enumChoices, setEnumChoices] = useState("");
  const [many, setMany] = useState(false);
  const [volatility, setVolatility] = useState("stable");
  const [error, setError] = useState("");
  const freshness =
    volatility === "dynamic" ? 90 : volatility === "slow" ? 365 : null;
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      const choices = enumChoices
        .split(",")
        .map((choice) => choice.trim())
        .filter(Boolean);
      const distinctChoices = [...new Set(choices)];
      if (type === "Enum" && distinctChoices.length < 2) {
        setError("Enum fields need at least two distinct choices.");
        return;
      }
      if (
        type === "Enum" &&
        distinctChoices.some(
          (choice) => choice.includes("[") || choice.includes("]"),
        )
      ) {
        setError("Enum choices cannot contain square brackets.");
        return;
      }
      const valueType =
        type === "Enum" ? `Enum[${distinctChoices.join(",")}]` : type;
      await post("/api/questions", {
        name,
        subject_kind: kind,
        value_type: valueType,
        definition: {
          label: label || name,
          cardinality: many ? "many" : "one",
          volatility,
          freshness_days: freshness,
        },
      });
      onClose();
      await onSaved();
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not add field",
      );
    }
  };
  return (
    <Modal
      title="Add research field"
      subtitle="Fields are typed, versioned questions—not mutable SQLite columns."
      onClose={onClose}
    >
      <form onSubmit={(event) => void submit(event)}>
        <div className="form-grid">
          <label>
            Field key
            <input
              value={name}
              onChange={(event) =>
                setName(
                  event.target.value.toLowerCase().replace(/[^a-z0-9_]/g, "_"),
                )
              }
              placeholder="funding_total"
              autoFocus
              required
            />
          </label>
          <label>
            Display label
            <input
              value={label}
              onChange={(event) => setLabel(event.target.value)}
              placeholder="Total funding"
            />
          </label>
        </div>
        <div className="form-grid">
          <label>
            Value type
            <select
              value={type}
              onChange={(event) => setType(event.target.value)}
            >
              <option>String</option>
              <option>Int</option>
              <option>Float</option>
              <option>Probability</option>
              <option>Bool</option>
              <option value="Enum">Enum · fixed choices</option>
              <option>Json</option>
              <option>Distribution[Float]</option>
            </select>
          </label>
          <label>
            How quickly can this change?
            <select
              value={volatility}
              onChange={(event) => setVolatility(event.target.value)}
            >
              <option value="stable">Stable fact · no expiry</option>
              <option value="slow">Changes occasionally · 1 year</option>
              <option value="dynamic">Current/dynamic · 90 days</option>
            </select>
          </label>
        </div>
        {type === "Enum" && (
          <label>
            Allowed choices
            <input
              value={enumChoices}
              onChange={(event) => setEnumChoices(event.target.value)}
              placeholder="standard, optional, unavailable, unknown"
              required
            />
            <span className="field-hint">
              Comma-separated. Agents and manual entries must return exactly one
              of these values.
            </span>
          </label>
        )}
        <label className="checkbox">
          <input
            type="checkbox"
            checked={many}
            onChange={(event) => setMany(event.target.checked)}
          />
          Allow multiple simultaneous answers
        </label>
        {error && <div className="form-error">{error}</div>}
        <div className="modal-actions">
          <button type="button" className="ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="primary">Add field</button>
        </div>
      </form>
    </Modal>
  );
}

function ClaimDialog({
  selection,
  onClose,
  onSaved,
}: {
  selection: Selection;
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const [value, setValue] = useState("");
  const [sourceType, setSourceType] = useState("web");
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [excerpt, setExcerpt] = useState("");
  const [confidence, setConfidence] = useState("high");
  const [error, setError] = useState("");
  const enumOptions = selection.question.value_type.startsWith("Enum[")
    ? selection.question.value_type.slice(5, -1).split(",")
    : [];
  const sourceLabels: Record<string, string> = {
    web: "Web page",
    personal: "Personal knowledge",
    model: "Model output",
    report: "Report or document",
    interview: "Interview or conversation",
    other: "Other evidence",
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      const evidence = await post<{ evidence_id: string }>("/api/evidence", {
        source_type: sourceType,
        url: url || null,
        title: title || sourceLabels[sourceType],
        excerpt,
        retrieved_at: today(),
      });
      await post("/api/claims", {
        subject: selection.entityId,
        question: selection.question.question_id,
        value: parseValue(value, selection.question.value_type),
        valid_from: today(),
        evidence_ids: [evidence.evidence_id],
        confidence,
      });
      onClose();
      await onSaved();
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not save answer",
      );
    }
  };
  return (
    <Modal
      title={`Answer ${String(selection.question.definition.label ?? selection.question.name)}`}
      subtitle={`${selection.entityName} · ${selection.question.value_type}. Existing answers remain in history.`}
      onClose={onClose}
    >
      <form onSubmit={(event) => void submit(event)}>
        <label>
          Answer
          {selection.question.value_type === "Bool" ? (
            <select
              value={value}
              onChange={(event) => setValue(event.target.value)}
              autoFocus
              required
            >
              <option value="">Choose…</option>
              <option value="true">Yes</option>
              <option value="false">No</option>
            </select>
          ) : enumOptions.length > 0 ? (
            <select
              value={value}
              onChange={(event) => setValue(event.target.value)}
              autoFocus
              required
            >
              <option value="">Choose…</option>
              {enumOptions.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
          ) : (
            <input
              value={value}
              onChange={(event) => setValue(event.target.value)}
              placeholder={
                selection.question.value_type === "Json"
                  ? '{"amount":12000000}'
                  : "Enter answer"
              }
              autoFocus
              required
            />
          )}
        </label>
        <div className="form-grid">
          <label>
            Evidence kind
            <select
              value={sourceType}
              onChange={(event) => {
                setSourceType(event.target.value);
                setTitle(sourceLabels[event.target.value]);
              }}
            >
              <option value="web">Web page</option>
              <option value="personal">Personal knowledge</option>
              <option value="model">Model output</option>
              <option value="report">Report or document</option>
              <option value="interview">Interview or conversation</option>
              <option value="other">Other evidence</option>
            </select>
          </label>
          <label>
            Source title
            <input
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder={sourceLabels[sourceType]}
              required
            />
          </label>
        </div>
        {sourceType === "web" && (
          <label>
            Source URL
            <input
              type="url"
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              placeholder="https://…"
              required
            />
          </label>
        )}
        <label>
          {sourceType === "personal"
            ? "Basis for your knowledge"
            : sourceType === "model"
              ? "Model output or run details"
              : "Evidence excerpt"}
          <textarea
            value={excerpt}
            onChange={(event) => setExcerpt(event.target.value)}
            placeholder={
              sourceType === "personal"
                ? "How do you know this? Include enough context for another person to assess it."
                : sourceType === "model"
                  ? "Model, prompt/run reference, and the relevant output…"
                  : "The bounded passage supporting this answer…"
            }
            required
          />
        </label>
        <label>
          Confidence
          <select
            value={confidence}
            onChange={(event) => setConfidence(event.target.value)}
          >
            <option>high</option>
            <option>medium</option>
            <option>low</option>
          </select>
        </label>
        {error && <div className="form-error">{error}</div>}
        <div className="modal-actions">
          <button type="button" className="ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="primary">Save evidence-backed answer</button>
        </div>
      </form>
    </Modal>
  );
}

function NotFoundDialog({
  selection,
  onClose,
  onSaved,
}: {
  selection: Selection;
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const [query, setQuery] = useState("");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await post("/api/research/not-found", {
        subject: selection.entityId,
        question: selection.question.question_id,
        query,
        notes,
      });
      onClose();
      await onSaved();
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not record search",
      );
    }
  };
  return (
    <Modal
      title="Record an unsuccessful search"
      subtitle="NotFound means research was attempted. It does not assert a negative answer."
      onClose={onClose}
    >
      <form onSubmit={(event) => void submit(event)}>
        <label>
          Search query
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            autoFocus
            required
          />
        </label>
        <label>
          Research notes
          <textarea
            value={notes}
            onChange={(event) => setNotes(event.target.value)}
            required
          />
        </label>
        {error && <div className="form-error">{error}</div>}
        <div className="modal-actions">
          <button type="button" className="ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="primary">Record NotFound</button>
        </div>
      </form>
    </Modal>
  );
}

function ResearchDialog({
  kind,
  question,
  initialMode,
  initialTarget,
  onClose,
  onLaunch,
}: {
  kind: string;
  question: Question;
  initialMode: "fill_missing" | "add_evidence";
  initialTarget: { entityId: string; entityName: string } | null;
  onClose: () => void;
  onLaunch: (
    question: Question,
    mode: "fill_missing" | "add_evidence",
    instructions: string,
    entityIds: string[] | null,
  ) => Promise<void>;
}) {
  const [mode, setMode] = useState(initialMode);
  const [instructions, setInstructions] = useState("");
  const [scope, setScope] = useState(initialTarget ? "cell" : "column");
  const label = String(question.definition.label ?? question.name);
  return (
    <Modal
      title={`Research ${label}`}
      subtitle={`Launch a background agent for the ${kind} sheet. Existing values are never replaced.`}
      onClose={onClose}
    >
      {initialTarget && (
        <div className="scope-picker">
          <div className="eyebrow">RESEARCH SCOPE</div>
          <label>
            <input
              type="radio"
              name="scope"
              value="cell"
              checked={scope === "cell"}
              onChange={() => setScope("cell")}
            />
            <span>
              <b>This cell only</b>
              <small>
                {initialTarget.entityName} × {label}
              </small>
            </span>
          </label>
          <label>
            <input
              type="radio"
              name="scope"
              value="column"
              checked={scope === "column"}
              onChange={() => setScope("column")}
            />
            <span>
              <b>Entire column</b>
              <small>Every eligible {kind.toLowerCase()} row</small>
            </span>
          </label>
        </div>
      )}
      <div className="research-modes">
        <button
          className={
            mode === "fill_missing" ? "mode-card selected" : "mode-card"
          }
          onClick={() => setMode("fill_missing")}
        >
          <b>Fill missing cells</b>
          <span>
            Research only Unasked rows and save evidence-backed answers.
          </span>
        </button>
        <button
          className={
            mode === "add_evidence" ? "mode-card selected" : "mode-card"
          }
          onClick={() => setMode("add_evidence")}
        >
          <b>Get more evidence</b>
          <span>Keep current values and attach new independent sources.</span>
        </button>
      </div>
      <label>
        Optional research instructions
        <textarea
          value={instructions}
          onChange={(event) => setInstructions(event.target.value)}
          placeholder="For example: prefer official biographies and university sources; avoid aggregators."
        />
      </label>
      <div className="research-disclosure">
        <span>✦</span>
        <p>
          <b>
            Will affect:{" "}
            {scope === "cell" && initialTarget
              ? initialTarget.entityName
              : `eligible rows in the ${label} column`}
          </b>
          <br />
          Search and validation status will update live. Epiq does not expose
          private model reasoning.
        </p>
      </div>
      <div className="modal-actions">
        <button className="ghost" onClick={onClose}>
          Cancel
        </button>
        <button
          className="primary"
          onClick={() =>
            void onLaunch(
              question,
              mode,
              instructions,
              scope === "cell" && initialTarget
                ? [initialTarget.entityId]
                : null,
            )
          }
        >
          Launch research →
        </button>
      </div>
    </Modal>
  );
}

function RowResearchDialog({
  kind,
  target,
  onClose,
  onLaunch,
}: {
  kind: string;
  target: { entityId: string; entityName: string; missing: number };
  onClose: () => void;
  onLaunch: (instructions: string) => Promise<void>;
}) {
  const [instructions, setInstructions] = useState("");
  return (
    <Modal
      title={`Research ${target.entityName}`}
      subtitle={`Try to answer every unasked research field for this ${kind.toLowerCase()}. Existing answers are left unchanged.`}
      onClose={onClose}
    >
      <div className="row-research-summary">
        <span>✦</span>
        <div>
          <b>
            {target.missing} unanswered field{target.missing === 1 ? "" : "s"}
          </b>
          <small>
            Epiq will run one focused, evidence-backed research job per field.
          </small>
        </div>
      </div>
      <label>
        Optional instructions for every field
        <textarea
          value={instructions}
          onChange={(event) => setInstructions(event.target.value)}
          placeholder="For example: prioritize primary sources and official biographies."
        />
      </label>
      <div className="research-disclosure">
        <span>◎</span>
        <p>
          <b>Scope: {target.entityName} only</b>
          <br />
          Each answer must pass its field’s type validation and include
          supporting evidence.
        </p>
      </div>
      <div className="modal-actions">
        <button className="ghost" onClick={onClose}>
          Cancel
        </button>
        <button
          className="primary"
          disabled={target.missing === 0}
          onClick={() => void onLaunch(instructions)}
        >
          Research {target.missing} field{target.missing === 1 ? "" : "s"} →
        </button>
      </div>
    </Modal>
  );
}

function ActivityPanel({
  jobs,
  onClose,
  onSuggestion,
  onAcceptFields,
}: {
  jobs: ResearchJob[];
  onClose: () => void;
  onSuggestion: (
    jobId: string,
    suggestionId: string,
    action: "accept" | "dismiss",
  ) => Promise<void>;
  onAcceptFields: (jobId: string, suggestionIds: string[]) => Promise<void>;
}) {
  return (
    <aside className="activity-panel">
      <div className="drawer-head">
        <div className="eyebrow">RESEARCH ACTIVITY</div>
        <button className="close" onClick={onClose}>
          ×
        </button>
        <h2>Background jobs</h2>
        <p>The spreadsheet remains stable while agents work.</p>
      </div>
      <div className="activity-list">
        {jobs.length === 0 && (
          <div className="drawer-empty">
            <div>✦</div>
            <p>No research jobs in this server session.</p>
          </div>
        )}
        {jobs.map((job) => (
          <article className="activity-job" key={job.job_id}>
            <div className="activity-job-head">
              <span className={`job-status ${job.status}`}>{job.status}</span>
              <code>
                {job.mode === "suggest_entities" ||
                job.mode === "suggest_fields"
                  ? job.entity_kind
                  : job.question_id.replace(/^q_|_v\d+$/g, "")}
              </code>
            </div>
            <b>
              {job.mode === "suggest_entities"
                ? `Suggest more ${job.entity_kind.toLowerCase()} rows`
                : job.mode === "suggest_fields"
                  ? `Suggest fields for ${job.entity_kind}`
                  : job.mode === "retry_not_found"
                    ? "Correct and retry NotFound"
                    : job.mode === "add_evidence"
                      ? "Get more evidence"
                      : "Fill missing values"}
            </b>
            <div className="job-progress">
              <i
                style={{
                  width: `${job.total ? (job.completed / job.total) * 100 : 8}%`,
                }}
              />
            </div>
            <small>
              {job.completed}/{job.total || "…"}{" "}
              {job.mode === "suggest_entities" || job.mode === "suggest_fields"
                ? "suggestions"
                : "rows"}
            </small>
            <ol>
              {job.messages.slice(-5).map((message, index) => (
                <li key={`${message.at}-${index}`}>{message.message}</li>
              ))}
            </ol>
            {job.error && <div className="form-error">{job.error}</div>}
            {job.suggestions?.map((suggestion) => (
              <div className="suggestion-card" key={suggestion.suggestion_id}>
                <div>
                  <b>{suggestion.name}</b>
                  <span className={`suggestion-state ${suggestion.status}`}>
                    {suggestion.status}
                  </span>
                </div>
                <p>{suggestion.rationale}</p>
                <a
                  href={suggestion.source_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  ↗ {suggestion.source_title}
                </a>
                {suggestion.status === "pending" && (
                  <div className="suggestion-actions">
                    <button
                      className="primary"
                      onClick={() =>
                        void onSuggestion(
                          job.job_id,
                          suggestion.suggestion_id,
                          "accept",
                        )
                      }
                    >
                      Add row
                    </button>
                    <button
                      onClick={() =>
                        void onSuggestion(
                          job.job_id,
                          suggestion.suggestion_id,
                          "dismiss",
                        )
                      }
                    >
                      Dismiss
                    </button>
                  </div>
                )}
              </div>
            ))}
            {job.field_suggestions && job.field_suggestions.length > 0 && (
              <FieldSuggestionReview job={job} onAccept={onAcceptFields} />
            )}
          </article>
        ))}
      </div>
    </aside>
  );
}

function FieldSuggestionReview({
  job,
  onAccept,
}: {
  job: ResearchJob;
  onAccept: (jobId: string, suggestionIds: string[]) => Promise<void>;
}) {
  const pending = job.field_suggestions?.filter(
    (suggestion) => suggestion.status === "pending",
  );
  const [selected, setSelected] = useState<string[]>([]);

  useEffect(() => {
    setSelected((current) => {
      const valid = new Set(pending?.map((item) => item.suggestion_id) ?? []);
      const retained = current.filter((item) => valid.has(item));
      if (retained.length > 0 || current.length > 0) return retained;
      return [...valid];
    });
  }, [job.field_suggestions]);

  const toggle = (suggestionId: string) =>
    setSelected((current) =>
      current.includes(suggestionId)
        ? current.filter((item) => item !== suggestionId)
        : [...current, suggestionId],
    );

  return (
    <div className="field-suggestion-review">
      {job.field_suggestions?.map((suggestion) => (
        <label
          className={`field-suggestion-card ${suggestion.status}`}
          key={suggestion.suggestion_id}
        >
          <input
            type="checkbox"
            checked={
              suggestion.status === "accepted" ||
              selected.includes(suggestion.suggestion_id)
            }
            disabled={suggestion.status !== "pending"}
            onChange={() => toggle(suggestion.suggestion_id)}
          />
          <span>
            <b>{suggestion.label}</b>
            <code>
              {suggestion.name} · {suggestion.value_type}
            </code>
            <p>{suggestion.rationale}</p>
            {suggestion.research_guidance && (
              <small>{suggestion.research_guidance}</small>
            )}
          </span>
        </label>
      ))}
      {!!pending?.length && (
        <button
          className="primary add-selected-fields"
          disabled={selected.length === 0}
          onClick={() => void onAccept(job.job_id, selected)}
        >
          Add {selected.length} selected field{selected.length === 1 ? "" : "s"}
        </button>
      )}
    </div>
  );
}

function SuggestEntitiesDialog({
  kind,
  onClose,
  onLaunch,
}: {
  kind: string;
  onClose: () => void;
  onLaunch: (count: number, instructions: string) => Promise<void>;
}) {
  const [count, setCount] = useState(5);
  const [instructions, setInstructions] = useState("");
  return (
    <Modal
      title={`Suggest more ${kind.toLowerCase()} rows`}
      subtitle="An agent will search for candidates. Nothing is added until you approve it."
      onClose={onClose}
    >
      <label>
        Number of suggestions
        <input
          type="number"
          min={1}
          max={20}
          value={count}
          onChange={(event) => setCount(Number(event.target.value))}
        />
      </label>
      <label>
        What kinds should it look for? <span>Optional</span>
        <textarea
          value={instructions}
          onChange={(event) => setInstructions(event.target.value)}
          placeholder={`For example: early-stage technology ${kind.toLowerCase()}s in the United States, with a mix of backgrounds.`}
        />
      </label>
      <div className="research-disclosure">
        <span>✦</span>
        <p>
          Candidates and discovery sources appear in Activity for individual
          review. Accepted candidates become ordinary rows with empty research
          cells.
        </p>
      </div>
      <div className="modal-actions">
        <button className="ghost" onClick={onClose}>
          Cancel
        </button>
        <button
          className="primary"
          disabled={count < 1 || count > 20}
          onClick={() => void onLaunch(count, instructions)}
        >
          Find candidates →
        </button>
      </div>
    </Modal>
  );
}

function SuggestFieldsDialog({
  kind,
  fieldCount,
  onClose,
  onLaunch,
}: {
  kind: string;
  fieldCount: number;
  onClose: () => void;
  onLaunch: (count: number, instructions: string) => Promise<void>;
}) {
  const [count, setCount] = useState(5);
  const [instructions, setInstructions] = useState("");
  return (
    <Modal
      title={`Suggest fields for ${kind}`}
      subtitle={`An agent will use your ${fieldCount} existing field${fieldCount === 1 ? "" : "s"} as context. Nothing is added until you check and approve it.`}
      onClose={onClose}
    >
      <label>
        Number of suggestions
        <input
          type="number"
          min={1}
          max={20}
          value={count}
          onChange={(event) => setCount(Number(event.target.value))}
        />
      </label>
      <label>
        What should the new fields help you understand? <span>Optional</span>
        <textarea
          value={instructions}
          onChange={(event) => setInstructions(event.target.value)}
          placeholder="For example: suggest fields useful for comparing investment strategy, track record, and sector focus."
        />
      </label>
      <div className="research-disclosure">
        <span>✦</span>
        <p>
          Each proposal includes a type and research guidance. Review the
          proposals as checkboxes in Activity, then add only the useful ones.
          New fields start with empty cells.
        </p>
      </div>
      <div className="modal-actions">
        <button className="ghost" onClick={onClose}>
          Cancel
        </button>
        <button
          className="primary"
          disabled={count < 1 || count > 20}
          onClick={() => void onLaunch(count, instructions)}
        >
          Design fields →
        </button>
      </div>
    </Modal>
  );
}

function RetireQuestionDialog({
  question,
  onClose,
  onSaved,
}: {
  question: Question;
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const label = String(question.definition.label ?? question.name);
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSaving(true);
    try {
      await post(`/api/questions/${question.question_id}/retire`, { reason });
      await onSaved();
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not remove field",
      );
      setSaving(false);
    }
  };
  return (
    <Modal
      title={`Remove “${label}”?`}
      subtitle="The field disappears from the current spreadsheet, but its answers, evidence, and event history are preserved."
      onClose={onClose}
    >
      <form onSubmit={(event) => void submit(event)}>
        <div className="retire-field-explanation">
          <span>Archive, don’t erase</span>
          <p>
            This records a <code>question.retire</code> event. Agents cannot add
            new answers to the retired field. It can be restored later with
            <code> epiq restore-question {question.name}</code>.
          </p>
        </div>
        <label>
          Why are you removing this field?
          <textarea
            required
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="For example: this asks for an aggregate rating, not a probability distribution."
          />
        </label>
        {error && <div className="form-error">{error}</div>}
        <div className="modal-actions">
          <button type="button" className="ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="danger-button" disabled={saving || !reason.trim()}>
            {saving ? "Removing…" : "Remove field"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function PolicyDialog({
  question,
  onClose,
  onSaved,
}: {
  question: Question;
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const [volatility, setVolatility] = useState(
    String(question.definition.volatility ?? "stable"),
  );
  const [days, setDays] = useState(
    String(question.definition.freshness_days ?? 90),
  );
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await post(`/api/questions/${question.question_id}/policy`, {
        volatility,
        freshness_days: volatility === "stable" ? null : Number(days),
      });
      onClose();
      await onSaved();
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Could not update time policy",
      );
    }
  };
  return (
    <Modal
      title="Field time policy"
      subtitle={String(question.definition.label ?? question.name)}
      onClose={onClose}
    >
      <form onSubmit={(event) => void submit(event)}>
        <label>
          How quickly can this answer change?
          <select
            value={volatility}
            onChange={(event) => {
              setVolatility(event.target.value);
              if (event.target.value === "dynamic") setDays("90");
              if (event.target.value === "slow") setDays("365");
            }}
          >
            <option value="stable">Stable fact · does not expire</option>
            <option value="slow">Changes occasionally</option>
            <option value="dynamic">Current or frequently changing</option>
          </select>
        </label>
        {volatility !== "stable" && (
          <label>
            Consider stale after
            <input
              type="number"
              min="1"
              value={days}
              onChange={(event) => setDays(event.target.value)}
              required
            />
            <span className="field-hint">
              days after the claim’s as-of date
            </span>
          </label>
        )}
        {error && <div className="form-error">{error}</div>}
        <div className="modal-actions">
          <button type="button" className="ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="primary">Save time policy</button>
        </div>
      </form>
    </Modal>
  );
}

function ChallengeDialog({
  kind,
  selection,
  claimId,
  onClose,
  onSaved,
}: {
  kind: string;
  selection: Selection;
  claimId: string;
  onClose: () => void;
  onSaved: (job: ResearchJob | null) => Promise<void>;
}) {
  const [reason, setReason] = useState("");
  const [guidance, setGuidance] = useState("");
  const [retract, setRetract] = useState(true);
  const [researchAgain, setResearchAgain] = useState(true);
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      const result = await post<{
        question_id: string;
        subject_id: string;
      }>(`/api/claims/${claimId}/challenge`, {
        reason,
        research_guidance: guidance,
        retract,
      });
      const job = researchAgain
        ? await post<ResearchJob>("/api/research/jobs", {
            entity_kind: kind,
            question: result.question_id,
            mode: "fill_missing",
            instructions: `Address this human correction: ${reason}`,
            entity_ids: [result.subject_id],
            scope: "cell",
          })
        : null;
      await onSaved(job);
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not challenge claim",
      );
    }
  };
  return (
    <Modal
      title="Challenge this answer"
      subtitle={`${selection.entityName} · ${String(selection.question.definition.label ?? selection.question.name)}`}
      onClose={onClose}
    >
      <form onSubmit={(event) => void submit(event)}>
        <label>
          What is wrong with this answer or its interpretation?
          <textarea
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="For example: birthplace is not equivalent to citizenship at birth."
            autoFocus
            required
          />
        </label>
        <label>
          Guidance for future research on this field
          {Boolean(selection.question.definition.research_guidance) && (
            <span className="existing-guidance">
              Existing:{" "}
              {String(selection.question.definition.research_guidance)}
            </span>
          )}
          <textarea
            value={guidance}
            onChange={(event) => setGuidance(event.target.value)}
            placeholder="Define the decision rule and evidence needed. Distinguish citizenship at birth from place of birth and naturalization."
          />
          <span className="field-hint">
            This versions the field definition and is sent to every future
            research agent.
          </span>
        </label>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={retract}
            onChange={(event) => setRetract(event.target.checked)}
          />
          Retract this answer from the current projection
        </label>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={researchAgain}
            onChange={(event) => setResearchAgain(event.target.checked)}
          />
          Research this cell again using the correction
        </label>
        {error && <div className="form-error">{error}</div>}
        <div className="modal-actions">
          <button type="button" className="ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="primary">Record challenge</button>
        </div>
      </form>
    </Modal>
  );
}

const challengeProblems = [
  ["modal_ambiguity", "Possibility vs. actuality"],
  ["type_mismatch", "Wrong answer type"],
  ["cardinality_mismatch", "One vs. many values"],
  ["temporal_mismatch", "Wrong time semantics"],
  ["level_mismatch", "Model vs. individual instance"],
  ["population_mismatch", "Does not apply to every row"],
  ["predicate_conflation", "Combines separate questions"],
  ["unit_mismatch", "Wrong or ambiguous unit"],
  ["epistemic_mismatch", "Unknown confused with false"],
  ["definition_ambiguity", "Ambiguous definition"],
  ["other", "Other category error"],
];

function ResearchOutcomeChallengeDialog({
  kind,
  selection,
  onClose,
  onSaved,
}: {
  kind: string;
  selection: Selection;
  onClose: () => void;
  onSaved: (job: ResearchJob) => Promise<void>;
}) {
  const [reason, setReason] = useState("");
  const [guidance, setGuidance] = useState("");
  const [saveToField, setSaveToField] = useState(true);
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      const result = await post<{ question_id: string; subject_id: string }>(
        `/api/research/${selection.cell.research?.task_id}/feedback`,
        {
          reason,
          research_guidance: guidance,
          save_to_field: saveToField,
        },
      );
      const job = await post<ResearchJob>("/api/research/jobs", {
        entity_kind: kind,
        question: result.question_id,
        mode: "retry_not_found",
        instructions: `Human challenge to the prior NotFound outcome: ${reason}\nInterpretation guidance: ${guidance}`,
        entity_ids: [result.subject_id],
        scope: "cell",
      });
      await onSaved(job);
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Could not challenge research outcome",
      );
    }
  };
  return (
    <Modal
      title="Challenge NotFound outcome"
      subtitle={`${selection.entityName} · ${String(selection.question.definition.label ?? selection.question.name)}`}
      onClose={onClose}
    >
      <form onSubmit={(event) => void submit(event)}>
        <div className="research-disclosure negative-evidence-note">
          <span>⊘</span>
          <p>
            A missing mention is usually inconclusive. But absence from an
            authoritative, exhaustive list can support a negative answer when
            its scope and date match the question.
          </p>
        </div>
        <label>
          Why is the NotFound interpretation wrong?
          <textarea
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="The official AEA recipient list is complete through 2026, so absence from it is evidence that the person did not win the medal."
            autoFocus
            required
          />
        </label>
        <label>
          Guidance for the retry
          <textarea
            value={guidance}
            onChange={(event) => setGuidance(event.target.value)}
            placeholder="Treat an authoritative complete recipient list as closed-world evidence. Cite the list and return False when the person is absent."
            required
          />
        </label>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={saveToField}
            onChange={(event) => setSaveToField(event.target.checked)}
          />
          Apply this interpretation to future research on this field
        </label>
        {error && <div className="form-error">{error}</div>}
        <div className="modal-actions">
          <button type="button" className="ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="primary">Record challenge and retry</button>
        </div>
      </form>
    </Modal>
  );
}

function QuestionChallengeDialog({
  question,
  rows,
  onClose,
  onSaved,
}: {
  question: Question;
  rows: Matrix["rows"];
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const [problem, setProblem] = useState("modal_ambiguity");
  const [explanation, setExplanation] = useState("");
  const [exampleEntity, setExampleEntity] = useState("");
  const [replacement, setReplacement] = useState("");
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      let proposedReplacement = null;
      if (replacement.trim()) {
        proposedReplacement = JSON.parse(replacement);
      }
      await post(`/api/questions/${question.question_id}/challenges`, {
        problem,
        explanation,
        example_entity: exampleEntity || null,
        evidence_ids: [],
        proposed_replacement: proposedReplacement,
      });
      await onSaved();
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Could not record field challenge",
      );
    }
  };
  return (
    <Modal
      title="Challenge field schema"
      subtitle={`${String(question.definition.label ?? question.name)} · ${question.value_type}`}
      onClose={onClose}
    >
      <form onSubmit={(event) => void submit(event)}>
        <label>
          What kind of category error is this?
          <select
            value={problem}
            onChange={(event) => setProblem(event.target.value)}
          >
            {challengeProblems.map(([value, label]) => (
              <option value={value} key={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Why can’t this field represent what you observed?
          <textarea
            value={explanation}
            onChange={(event) => setExplanation(event.target.value)}
            placeholder="For example: this Boolean conflates whether a model can be equipped with a spinnaker and whether a particular boat currently has one."
            autoFocus
            required
          />
        </label>
        <label>
          Counterexample row <span>Optional</span>
          <select
            value={exampleEntity}
            onChange={(event) => setExampleEntity(event.target.value)}
          >
            <option value="">No specific row</option>
            {rows.map((row) => (
              <option key={row.entity_id} value={row.entity_id}>
                {row.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Proposed replacement schema <span>Optional JSON</span>
          <textarea
            className="mono-input"
            value={replacement}
            onChange={(event) => setReplacement(event.target.value)}
            placeholder={
              '{"name":"spinnaker_availability","value_type":"Enum[standard,optional,unavailable,unknown]"}'
            }
          />
          <span className="field-hint">
            This is a review proposal only. It will not add, remove, or migrate
            columns.
          </span>
        </label>
        {error && <div className="form-error">{error}</div>}
        <div className="modal-actions">
          <button type="button" className="ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="primary">Record schema challenge</button>
        </div>
      </form>
    </Modal>
  );
}

function SchemaReviewPanel({
  challenges,
  onClose,
  onChanged,
}: {
  challenges: QuestionChallenge[];
  onClose: () => void;
  onChanged: () => Promise<void>;
}) {
  const resolve = async (
    challenge: QuestionChallenge,
    status: "resolved" | "dismissed",
  ) => {
    const resolution = window.prompt(
      status === "resolved"
        ? "How was this schema issue resolved?"
        : "Why dismiss this challenge?",
    );
    if (!resolution) return;
    await post(`/api/question-challenges/${challenge.challenge_id}/resolve`, {
      status,
      resolution,
    });
    await onChanged();
  };
  return (
    <aside className="activity-panel schema-review-panel">
      <div className="drawer-head">
        <div className="eyebrow">SCHEMA REVIEW</div>
        <button className="close" onClick={onClose}>
          ×
        </button>
        <h2>Open field challenges</h2>
        <p>
          Counterexamples that suggest the table’s categories need revision.
        </p>
      </div>
      <div className="activity-list">
        {challenges.length === 0 && (
          <div className="drawer-empty">
            <div>✓</div>
            <p>No open schema challenges.</p>
          </div>
        )}
        {challenges.map((challenge) => (
          <article className="schema-review-card" key={challenge.challenge_id}>
            <span>{challenge.problem.replaceAll("_", " ")}</span>
            <h3>{challenge.question_name}</h3>
            <p>{challenge.explanation}</p>
            {challenge.example_entity_name && (
              <small>Counterexample: {challenge.example_entity_name}</small>
            )}
            {challenge.proposed_replacement && (
              <pre>
                {JSON.stringify(challenge.proposed_replacement, null, 2)}
              </pre>
            )}
            <div>
              <button
                className="primary"
                onClick={() => void resolve(challenge, "resolved")}
              >
                Resolve
              </button>
              <button onClick={() => void resolve(challenge, "dismissed")}>
                Dismiss
              </button>
            </div>
          </article>
        ))}
      </div>
    </aside>
  );
}

function CellDrawer({
  selection,
  isResearching,
  onClose,
  onAdd,
  onNotFound,
  onEnrich,
  onResearch,
  onPolicy,
  onSchemaChallenge,
  onChallengeResearch,
  onChallenge,
  onChanged,
}: {
  selection: Selection;
  isResearching: boolean;
  onClose: () => void;
  onAdd: () => void;
  onNotFound: () => void;
  onEnrich: () => void;
  onResearch: () => void;
  onPolicy: () => void;
  onSchemaChallenge: () => void;
  onChallengeResearch: () => void;
  onChallenge: (claimId: string) => void;
  onChanged: () => Promise<void>;
}) {
  const { cell, entityName, question } = selection;
  const [busy, setBusy] = useState("");
  const retract = async (claimId: string) => {
    const reason = window.prompt("Why are you retracting this claim?");
    if (!reason) return;
    setBusy(claimId);
    await post(`/api/claims/${claimId}/retract`, { reason });
    await onChanged();
  };
  const claims = [
    ...cell.lineage
      .reduce((grouped, item) => {
        const group = grouped.get(item.claim_id);
        if (group) group.evidence.push(item);
        else grouped.set(item.claim_id, { claim: item, evidence: [item] });
        return grouped;
      }, new Map<string, { claim: (typeof cell.lineage)[number]; evidence: typeof cell.lineage }>())
      .values(),
  ];
  return (
    <aside className="drawer">
      <div className="drawer-head">
        <div className="eyebrow">CELL INSPECTOR</div>
        <button className="close" onClick={onClose}>
          ×
        </button>
        <h2>{String(question.definition.label ?? question.name)}</h2>
        <p>
          {entityName} · <code>{question.value_type}</code>
        </p>
        <button className="schema-challenge-link" onClick={onSchemaChallenge}>
          {question.schema_state === "challenged"
            ? "⚠ Review field challenge"
            : "? Challenge field schema"}
        </button>
      </div>
      <div className="drawer-body">
        <div
          className={`status-card ${isResearching ? "state-researching" : `state-${cell.state.toLowerCase()}`}`}
        >
          {isResearching ? (
            <span className="row-spinner" />
          ) : (
            <span className="state-dot" />
          )}
          <div>
            <small>CURRENT STATE</small>
            <b>{isResearching ? "Researching" : cell.state}</b>
            {isResearching && <em>Searching and validating evidence…</em>}
          </div>
        </div>
        {cell.temporal?.freshness === "stale" && (
          <div className="stale-warning">
            <b>⚠ Evidence may be stale</b>
            <span>
              Supported as of {cell.temporal.as_of}; this field is expected to
              change within {cell.temporal.freshness_days} days.
            </span>
            <button onClick={onEnrich}>Refresh evidence</button>
          </div>
        )}
        {cell.temporal?.freshness === "unknown" && (
          <div className="stale-warning unknown">
            <b>◷ Currentness unknown</b>
            <span>
              The evidence supports this answer historically, but Epiq cannot
              establish when it was last true.
            </span>
            <button onClick={onEnrich}>Find current evidence</button>
          </div>
        )}
        {cell.temporal?.freshness === "fresh" && (
          <div className="as-of-note">Current as of {cell.temporal.as_of}</div>
        )}
        {Boolean(question.definition.research_guidance) && (
          <div className="field-guidance">
            <b>Research interpretation</b>
            <span>{String(question.definition.research_guidance)}</span>
          </div>
        )}
        {cell.state === "NotFound" && (
          <div className="research-card">
            <small>SEARCHED FOR</small>
            <code>{cell.research?.query}</code>
            <p>{cell.research?.notes}</p>
            <button className="challenge-outcome" onClick={onChallengeResearch}>
              Challenge this NotFound outcome
            </button>
          </div>
        )}
        {claims.map(({ claim, evidence }) => (
          <article className="claim-card" key={claim.claim_id}>
            <div className="claim-top">
              <span className={`confidence ${claim.confidence}`}>
                {claim.confidence}
              </span>
              <code>{claim.token}</code>
            </div>
            <div className="answer">{display(claim.value)}</div>
            {claim.as_of && (
              <div className="claim-as-of">
                Claim observed as of {claim.as_of}
              </div>
            )}
            <div className="evidence-heading">
              {evidence.length} supporting source
              {evidence.length === 1 ? "" : "s"}
            </div>
            {evidence.map((item, index) => (
              <div className="evidence-item" key={item.evidence_id}>
                <span className="evidence-number">{index + 1}</span>
                <div>
                  <blockquote>{item.excerpt}</blockquote>
                  {item.source.url.startsWith("http") ? (
                    <a href={item.source.url} target="_blank" rel="noreferrer">
                      ↗ {item.source.title}
                    </a>
                  ) : (
                    <div className="non-web-source">◈ {item.source.title}</div>
                  )}
                  <small className="source-dates">
                    {item.source.published_at
                      ? `Published ${item.source.published_at} · `
                      : "Publication date unknown · "}
                    Retrieved {item.source.retrieved_at}
                  </small>
                </div>
              </div>
            ))}
            <div className="claim-actions">
              <button onClick={() => onChallenge(claim.claim_id)}>
                Challenge answer
              </button>
              <button
                className="retract"
                disabled={busy === claim.claim_id}
                onClick={() => void retract(claim.claim_id)}
              >
                Retract only
              </button>
            </div>
          </article>
        ))}
        {cell.state === "Unasked" && (
          <div className="drawer-empty">
            <div>?</div>
            <p>No answer or completed research has been recorded.</p>
          </div>
        )}
      </div>
      <div className="drawer-actions">
        {cell.state === "Answered" ? (
          <button className="ghost" onClick={onEnrich}>
            ✦ Get more evidence
          </button>
        ) : cell.state === "NotFound" ? (
          <>
            <button className="ghost" onClick={onChallengeResearch}>
              Challenge NotFound
            </button>
            <button className="primary" onClick={onChallengeResearch}>
              ✦ Correct and retry
            </button>
          </>
        ) : (
          <>
            <button className="ghost" onClick={onNotFound}>
              Mark NotFound
            </button>
            <button
              className="primary"
              onClick={onResearch}
              disabled={isResearching}
            >
              {isResearching ? "Researching…" : "✦ Research this cell"}
            </button>
          </>
        )}
        <button
          className={cell.state === "Answered" ? "primary" : "ghost"}
          onClick={onAdd}
        >
          ＋ Add evidence-backed answer
        </button>
      </div>
    </aside>
  );
}
