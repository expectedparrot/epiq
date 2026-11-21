import {
  FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ApiError,
  Cell,
  CuratorState,
  DiagnosticCell,
  DiagnosticResult,
  Matrix,
  Overview,
  ProjectInfo,
  QuestionChallenge,
  Question,
  RelationshipSuggestion,
  RelationshipGraph,
  RelatedEntity,
  ResearchJob,
  StaleDerivation,
  api,
  post,
} from "./api";
import { useTableData } from "./useTableData";
import { SchemaAdaptationReviews } from "./SchemaAdaptationReview";

type Selection = {
  entityId: string;
  entityName: string;
  question: Question;
  cell: Cell;
};
type ProvisionalRelationship = RelationshipSuggestion & { jobId: string };
type EntitySelection = {
  entityId: string;
  entityName: string;
  entityKind: string;
};
type SortState = {
  key: string;
  direction: "asc" | "desc";
};
type SavedView = {
  id: string;
  name: string;
  filterText: string;
  statusFilter: string;
  sort: SortState;
  wrapText: boolean;
  columnOrder: string[];
  columnWidths: Record<string, number>;
  hiddenColumns: string[];
  columnFormats: Record<string, NumberFormat>;
};
type NumberFormat = {
  use_grouping: boolean;
  precision: "decimal" | "significant";
  digits: number;
};
type PastedClaim = {
  entityId: string;
  entityName: string;
  question: Question;
  rawValue: string;
  existingState: string;
  value?: unknown;
  error?: string;
};

function mergeJobs(current: ResearchJob[], incoming: ResearchJob[]) {
  const byId = new Map(current.map((job) => [job.job_id, job]));
  incoming.forEach((job) => byId.set(job.job_id, job));
  const incomingIds = new Set(incoming.map((job) => job.job_id));
  return [
    ...incoming.filter(
      (job, index) =>
        incoming.findIndex((candidate) => candidate.job_id === job.job_id) ===
        index,
    ),
    ...current.filter((job) => !incomingIds.has(job.job_id)),
  ].map((job) => byId.get(job.job_id)!);
}

function jobRevision(jobs: ResearchJob[]) {
  return JSON.stringify(
    jobs.map((job) => [
      job.job_id,
      job.status,
      job.completed,
      job.written,
      job.messages.at(-1)?.at,
      job.child_job_ids?.length ?? 0,
      job.relationship_suggestions?.filter((item) => item.status === "pending")
        .length ?? 0,
    ]),
  );
}
type Dialog =
  | "entity"
  | "entityKind"
  | "question"
  | "claim"
  | "notFound"
  | "research"
  | "rowResearch"
  | "suggestEntities"
  | "suggestFields"
  | "policy"
  | "schemaChallenge"
  | "editQuestion"
  | "retireQuestion"
  | "mergeEntity"
  | "findDuplicates"
  | "paste"
  | "fill"
  | null;

const today = () => new Date().toISOString().slice(0, 10);
const integerFormat = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 0,
});
const spreadsheetColumn = (index: number) => {
  let value = index + 1;
  let label = "";
  while (value > 0) {
    value -= 1;
    label = String.fromCharCode(65 + (value % 26)) + label;
    value = Math.floor(value / 26);
  }
  return label;
};
const spreadsheetColumnIndex = (label: string) =>
  [...label.toUpperCase()].reduce(
    (total, character) => total * 26 + character.charCodeAt(0) - 64,
    0,
  ) - 1;
const parseSpreadsheetFormula = (expression: string, questions: Question[]) => {
  const normalized = expression.trim();
  if (!normalized.startsWith("="))
    throw new Error("Formulas must begin with =.");
  const references = [...normalized.matchAll(/([A-Z]+)(\d+)/gi)];
  if (!references.length)
    throw new Error("Formula must reference at least one research field.");
  if (new Set(references.map((match) => match[2])).size !== 1)
    throw new Error("All formula references must use the same row.");
  const inputs: string[] = [];
  const inputIndexes = new Map<string, number>();
  let compiled = normalized.slice(1).replace(/([A-Z]+)(\d+)/gi, (_, column) => {
    const question = questions[spreadsheetColumnIndex(column) - 1];
    if (!question) return "__INVALID_REFERENCE__";
    let inputIndex = inputIndexes.get(question.name);
    if (inputIndex === undefined) {
      inputIndex = inputs.length;
      inputIndexes.set(question.name, inputIndex);
      inputs.push(question.name);
    }
    return `x${inputIndex}`;
  });
  if (compiled.includes("__INVALID_REFERENCE__"))
    throw new Error(
      "Formula references must point to visible research fields, not the entity column.",
    );
  compiled = compiled.replace(/\s+/g, "");
  if (!/^[x0-9+\-*/().]+$/.test(compiled) || compiled.includes("**"))
    throw new Error(
      "Formulas support numbers, cell references, parentheses, and + − × ÷.",
    );
  return {
    operation: "expression",
    inputs,
    parameters: { expression: compiled },
    expression: normalized.toUpperCase(),
  };
};
const display = (value: unknown) => {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number" && Number.isInteger(value))
    return integerFormat.format(value);
  return String(value);
};
const formattedValue = (
  value: unknown,
  question?: Question | string,
  override?: NumberFormat,
) => {
  const valueType =
    typeof question === "string" ? question : question?.value_type;
  const definition =
    typeof question === "string" ? {} : (question?.definition ?? {});
  const format = (override ?? definition.display_format ?? {}) as Record<
    string,
    unknown
  >;
  const hasDisplayFormat = Boolean(override ?? definition.display_format);
  if (
    valueType?.startsWith("Quantity[") &&
    valueType.endsWith("]") &&
    value !== null &&
    typeof value === "object" &&
    "value" in value
  ) {
    const quantity = value as { value: unknown; unit?: unknown };
    const unit =
      typeof quantity.unit === "string"
        ? quantity.unit
        : valueType.slice("Quantity[".length, -1);
    const amount =
      typeof quantity.value === "number"
        ? new Intl.NumberFormat("en-US", {
            maximumFractionDigits: 6,
            useGrouping: format.use_grouping !== false,
          }).format(quantity.value)
        : display(quantity.value);
    return `${amount} ${unit}`.trim();
  }
  if (valueType === "Year" && typeof value === "number") return String(value);
  if (valueType === "Int" && typeof value === "number") {
    return new Intl.NumberFormat("en-US", {
      maximumFractionDigits: 0,
      useGrouping: format.use_grouping !== false,
    }).format(value);
  }
  if (
    (valueType === "Float" || valueType === "Probability") &&
    typeof value === "number" &&
    hasDisplayFormat
  ) {
    const digits = Math.max(1, Math.min(12, Number(format.digits ?? 3)));
    return new Intl.NumberFormat("en-US", {
      useGrouping: format.use_grouping !== false,
      ...(format.precision === "decimal"
        ? { maximumFractionDigits: digits }
        : { maximumSignificantDigits: digits }),
    }).format(value);
  }
  return display(value);
};
const cellDisplay = (
  cell: Cell,
  question?: Question | string,
  format?: NumberFormat,
) => {
  if (cell.references?.length)
    return cell.references.map((item) => item.name).join(", ");
  if (cell.state === "Answered") {
    const value = cell.value ?? cell.values;
    const rendered = formattedValue(value, question, format);
    return cell.lineage.some((item) => item.epistemic_status === "estimated")
      ? `≈ ${rendered}`
      : rendered;
  }
  if (cell.state === "Contested")
    return `${cell.values.length} competing answers`;
  if (cell.state === "NotFound") return "No evidence found";
  return "";
};
const cellStateLabel = (cell: Cell) =>
  cell.state === "Answered" &&
  cell.lineage.some((item) => item.epistemic_status === "estimated")
    ? "Estimated"
    : cell.state;

function parseValue(raw: string, type: string): unknown {
  if (
    type === "String" ||
    type === "URL" ||
    type === "Image" ||
    type.startsWith("Enum[") ||
    type.startsWith("Ref[")
  )
    return raw;
  if (type === "Date") {
    const normalized = raw.trim();
    const parsed = new Date(`${normalized}T00:00:00Z`);
    if (
      !/^\d{4}-\d{2}-\d{2}$/.test(normalized) ||
      Number.isNaN(parsed.valueOf()) ||
      parsed.toISOString().slice(0, 10) !== normalized
    )
      throw new Error("Expected a date in YYYY-MM-DD format");
    return normalized;
  }
  if (type === "DateTime") return raw.trim();
  if (type === "Year") {
    if (!/^\d{1,4}$/.test(raw.trim()))
      throw new Error("Expected a year from 1 to 9999");
    const value = Number.parseInt(raw, 10);
    if (value < 1 || value > 9999)
      throw new Error("Expected a year from 1 to 9999");
    return value;
  }
  if (type === "Int") {
    if (!/^-?\d+$/.test(raw.trim())) throw new Error("Expected a whole number");
    return Number.parseInt(raw, 10);
  }
  if (type === "Float" || type === "Probability") {
    const value = Number(raw);
    if (!Number.isFinite(value)) throw new Error("Expected a number");
    return value;
  }
  if (type === "Bool") {
    const normalized = raw.trim().toLowerCase();
    if (["true", "yes", "1"].includes(normalized)) return true;
    if (["false", "no", "0"].includes(normalized)) return false;
    throw new Error("Expected Yes/No or True/False");
  }
  return JSON.parse(raw);
}

export default function App() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [kind, setKind] = useState("");
  const [selection, setSelection] = useState<Selection | null>(null);
  const [entitySelection, setEntitySelection] =
    useState<EntitySelection | null>(null);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [needsInit, setNeedsInit] = useState(false);
  const [jobs, setJobs] = useState<ResearchJob[]>([]);
  const [curator, setCurator] = useState<CuratorState | null>(null);
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
  const [rightPane, setRightPane] = useState<
    "agent" | "inspect" | "activity" | "review" | null
  >("agent");
  const [chatDraft, setChatDraft] = useState("");
  const [centerView, setCenterView] = useState<"table" | "row">("table");
  const [reviewItems, setReviewItems] = useState<{
    stale: DiagnosticCell[];
    contradictions: DiagnosticCell[];
  }>({ stale: [], contradictions: [] });
  const [projectClosed, setProjectClosed] = useState(false);
  const [showProjects, setShowProjects] = useState(false);
  const [wrapText, setWrapText] = useState(true);
  const [columnOrder, setColumnOrder] = useState<string[]>([]);
  const [columnWidths, setColumnWidths] = useState<Record<string, number>>({});
  const [columnFormats, setColumnFormats] = useState<
    Record<string, NumberFormat>
  >({});
  const [hiddenColumns, setHiddenColumns] = useState<string[]>([]);
  const [savedViews, setSavedViews] = useState<SavedView[]>([]);
  const [activeViewId, setActiveViewId] = useState("");
  const [pastedClaims, setPastedClaims] = useState<PastedClaim[]>([]);
  const [formulaDragQuestion, setFormulaDragQuestion] =
    useState<Question | null>(null);
  const [draggedColumn, setDraggedColumn] = useState<string | null>(null);
  const [schemaChallengeQuestion, setSchemaChallengeQuestion] =
    useState<Question | null>(null);
  const [retireQuestion, setRetireQuestion] = useState<Question | null>(null);
  const [editQuestion, setEditQuestion] = useState<Question | null>(null);
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
  const [selectionAnchor, setSelectionAnchor] = useState<{
    entityId: string;
    questionId: string;
  } | null>(null);
  const [, setClipboardNotice] = useState("");
  const jobsRef = useRef<ResearchJob[]>([]);
  const jobsRevisionRef = useRef("");
  const { matrix, staleDerivations, loadMatrix, clearMatrix } = useTableData(
    overview?.project.project_id, kind, !needsInit && !projectClosed, setError,
  );
  const overviewGeneration = useRef(0);
  const activeProjectIdRef = useRef<string | null>(null);
  const layoutKey = `epiq-layout:${overview?.project.project_id ?? "project"}:${kind}`;
  const viewsKey = `epiq-views:${overview?.project.project_id ?? "project"}:${kind}`;
  const getColumnFormat = (question: Question): NumberFormat => {
    const stored = (question.definition.display_format ?? {}) as Record<
      string,
      unknown
    >;
    return (
      columnFormats[question.name] ?? {
        use_grouping: stored.use_grouping !== false,
        precision: stored.precision === "decimal" ? "decimal" : "significant",
        digits: Number(stored.digits ?? 3),
      }
    );
  };

  const loadOverview = useCallback(async () => {
    const generation = ++overviewGeneration.current;
    try {
      const next = await api<Overview>("/api/project");
      if (generation !== overviewGeneration.current) return;
      const primaryKind = [...next.entity_kinds].sort(
        (left, right) =>
          right.questions - left.questions || right.entities - left.entities,
      )[0]?.kind;
      const nextProjectId = next.project.project_id;
      const projectChanged =
        activeProjectIdRef.current !== null &&
        activeProjectIdRef.current !== nextProjectId;
      activeProjectIdRef.current = nextProjectId;
      if (projectChanged) {
        clearMatrix();
        setJobs([]);
        jobsRef.current = [];
        jobsRevisionRef.current = "";
        setSelection(null);
        setEntitySelection(null);
        setChatDraft("");
        setKind(primaryKind || "");
      } else {
        setKind((current) => current || primaryKind || "");
      }
      setOverview(next);
      setNeedsInit(false);
    } catch (caught) {
      if (generation !== overviewGeneration.current) return;
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
      if (generation === overviewGeneration.current) setLoading(false);
    }
  }, [clearMatrix]);

  const staleDerivedClaimIds = useMemo(
    () => new Set(staleDerivations.map((item) => item.claim_id)),
    [staleDerivations],
  );
  const loadReviewItems = useCallback(async () => {
    if (!kind || needsInit) return;
    try {
      const [stale, contradictions] = await Promise.all([
        api<DiagnosticResult>(`/api/stale/${encodeURIComponent(kind)}`),
        api<DiagnosticResult>(
          `/api/contradictions/${encodeURIComponent(kind)}`,
        ),
      ]);
      setReviewItems({
        stale: stale.cells,
        contradictions: contradictions.cells,
      });
    } catch {
      /* Review diagnostics do not block the spreadsheet. */
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
    void loadReviewItems();
  }, [loadReviewItems]);
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
      setColumnFormats(saved.columnFormats ?? {});
      setHiddenColumns(
        Array.isArray(saved.hiddenColumns) ? saved.hiddenColumns : [],
      );
      setSort(
        saved.sort?.key && ["asc", "desc"].includes(saved.sort.direction)
          ? saved.sort
          : { key: "__entity__", direction: "asc" },
      );
      setStatusFilter(
        ["all", "answered", "unanswered", "provisional", "review"].includes(
          saved.statusFilter,
        )
          ? saved.statusFilter
          : "all",
      );
    } catch {
      setWrapText(true);
      setColumnOrder([]);
      setColumnWidths({});
      setColumnFormats({});
      setHiddenColumns([]);
      setSort({ key: "__entity__", direction: "asc" });
      setStatusFilter("all");
    }
  }, [kind, overview, layoutKey]);
  useEffect(() => {
    try {
      const stored = JSON.parse(localStorage.getItem(viewsKey) ?? "[]");
      setSavedViews(Array.isArray(stored) ? stored : []);
      setActiveViewId("");
    } catch {
      setSavedViews([]);
      setActiveViewId("");
    }
  }, [viewsKey]);
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
    if (needsInit || projectClosed) return;
    let cancelled = false;
    let polling = false;
    const poll = async () => {
      if (polling) return;
      polling = true;
      try {
        const next = await api<ResearchJob[]>("/api/research/jobs");
        if (!cancelled) {
          const previous = new Map(
            jobsRef.current.map((job) => [job.job_id, job.status]),
          );
          const justFinished = next.find(
            (job) =>
              ["queued", "running"].includes(previous.get(job.job_id) ?? "") &&
              ["completed", "failed", "cancelled"].includes(job.status),
          );
          if (justFinished) {
            if (justFinished.job_type === "workspace_agent") {
              void loadOverview();
            }
          }
          jobsRef.current = next;
          const hasActive = next.some(
            (job) => job.status === "queued" || job.status === "running",
          );
          const nextRevision = jobRevision(next);
          const progressChanged = nextRevision !== jobsRevisionRef.current;
          jobsRevisionRef.current = nextRevision;
          const hadActive = [...previous.values()].some(
            (status) => status === "queued" || status === "running",
          );
          if (progressChanged || hasActive || hadActive) void loadMatrix(false);
          if (progressChanged && next.some((job) => job.job_type === "workspace_agent"))
            void loadOverview();
          setJobs(next);
        }
      } catch {
        /* The table remains usable if job polling is unavailable. */
      } finally {
        polling = false;
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 750);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [needsInit, projectClosed, loadMatrix, loadOverview]);
  useEffect(() => {
    if (needsInit || projectClosed) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const state = await api<CuratorState | null>("/api/curator");
        if (!cancelled) setCurator(state);
      } catch {
        /* Curator controls are optional while the project remains usable. */
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 1000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [needsInit, projectClosed]);
  useEffect(() => {
    if (needsInit || projectClosed) return;
    const refreshVisibleProject = () => {
      if (document.visibilityState !== "visible") return;
      void loadOverview();
      void loadMatrix(false);
    };
    const timer = window.setInterval(refreshVisibleProject, 5000);
    return () => window.clearInterval(timer);
  }, [needsInit, projectClosed, loadOverview, loadMatrix]);

  const refresh = async () => {
    setSelection(null);
    await loadOverview();
    await loadMatrix();
    await loadReviewItems();
  };
  const directWorkspaceAgent = async (message: string, autoApprove: boolean) => {
    const job = await post<ResearchJob>("/api/workspace-agent/jobs", {
      message,
      auto_approve: autoApprove,
    });
    setJobs((current) => mergeJobs(current, [job]));
    jobsRef.current = mergeJobs(jobsRef.current, [job]);
    setRightPane("agent");
  };
  const startCurator = async (
    objective: string,
    maxCycles: number,
    maxResearchCells: number,
  ) => {
    const state = await post<CuratorState>("/api/curator", {
      objective,
      max_cycles: maxCycles,
      max_research_cells: maxResearchCells,
    });
    setCurator(state);
  };
  const controlCurator = async (action: "pause" | "resume" | "stop") => {
    setCurator(await post<CuratorState>(`/api/curator/${action}`, {}));
  };
  const draftForAgent = (message: string) => {
    setChatDraft(message);
    setDialog(null);
    setRightPane("agent");
  };
  const approveWorkspacePlan = async (jobId: string) => {
    const job = await post<ResearchJob>(
      `/api/workspace-agent/jobs/${jobId}/approve`,
      {},
    );
    setJobs((current) => mergeJobs(current, [job]));
  };
  const rejectWorkspacePlan = async (jobId: string) => {
    const job = await post<ResearchJob>(
      `/api/workspace-agent/jobs/${jobId}/reject`,
      {},
    );
    setJobs((current) => mergeJobs(current, [job]));
  };
  const inspectDiagnostic = (item: DiagnosticCell) => {
    const row = matrix?.rows.find(
      (candidate) => candidate.entity_id === item.entity_id,
    );
    const question = matrix?.questions.find(
      (candidate) => candidate.question_id === item.question_id,
    );
    if (!row || !question) return;
    setSelection({
      entityId: row.entity_id,
      entityName: row.name,
      question,
      cell: row.cells[question.name],
    });
    setRightPane("inspect");
  };
  const projectReady = async () => {
    ++overviewGeneration.current;
    setKind("");
    clearMatrix();
    setOverview(null);
    setJobs([]);
    setCurator(null);
    jobsRef.current = [];
    jobsRevisionRef.current = "";
    setSelection(null);
    setEntitySelection(null);
    setChatDraft("");
    setProjectClosed(false);
    setShowProjects(false);
    await loadOverview();
  };
  const closeProject = async () => {
    try {
      await post("/api/projects/close", {});
      ++overviewGeneration.current;
      setOverview(null);
      clearMatrix();
      setKind("");
      setJobs([]);
      setCurator(null);
      jobsRef.current = [];
      jobsRevisionRef.current = "";
      setProjectClosed(true);
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not close project",
      );
    }
  };
  const materializeFormula = async (
    question: Question,
    subjects?: string[],
  ) => {
    try {
      await post("/api/materialize", {
        entity_kind: kind,
        question: question.name,
        valid_from: today(),
        ...(subjects ? { subjects } : {}),
      });
      await loadMatrix();
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not calculate field",
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
    const label = String(question.definition.label ?? question.name);
    const target = entityIds?.length
      ? `the ${label} field for ${researchTarget?.entityName ?? entityIds[0]}`
      : `the ${label} column in the ${kind} table`;
    const action = mode === "add_evidence"
      ? `Find and attach additional independent evidence for ${target}. Keep existing values.`
      : `Research and fill unanswered cells in ${target}. Keep existing answers.`;
    draftForAgent(`${action}${instructions.trim() ? `\n\nInstructions: ${instructions.trim()}` : ""}`);
  };
  const cancelResearch = async (jobId: string) => {
    try {
      await post(`/api/research/jobs/${jobId}/cancel`, {});
      setJobs(await api<ResearchJob[]>("/api/research/jobs"));
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not cancel research",
      );
    }
  };
  const cancelResearchScope = async (
    scope: "cell" | "row" | "column" | "table",
    entityId?: string,
    questionId?: string,
  ) => {
    try {
      const result = await post<{ count: number; jobs: ResearchJob[] }>(
        "/api/research/cancel",
        {
          scope,
          entity_kind: kind,
          entity_id: entityId,
          question_id: questionId,
        },
      );
      setJobs((current) => mergeJobs(current, result.jobs));
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not cancel research",
      );
    }
  };
  const retryResearch = async (jobId: string) => {
    try {
      const job = await post<ResearchJob>(
        `/api/research/jobs/${jobId}/retry`,
        {},
      );
      setJobs((current) => mergeJobs(current, [job]));
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not retry research",
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
  const activeResearchJobs = useMemo(
    () =>
      jobs.filter(
        (job) =>
          (job.status === "queued" || job.status === "running") &&
          (!job.job_type || job.job_type === "research") &&
          job.entity_kind === kind,
      ),
    [jobs, kind],
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
    draftForAgent(`Research every unanswered research cell in the ${kind} table. Keep existing answers and require evidence for every new value.`);
  };
  const launchRowResearch = async (instructions: string) => {
    if (!rowResearchTarget) return;
    draftForAgent(`Research every unanswered field for ${rowResearchTarget.entityName} in the ${kind} table. Keep existing answers.${instructions.trim() ? `\n\nInstructions: ${instructions.trim()}` : ""}`);
  };
  const launchSuggestions = async (count: number, instructions: string) => {
    draftForAgent(`Find up to ${count} additional rows for the ${kind} table matching this request: ${instructions.trim()}. Propose them for review before adding anything.`);
  };
  const launchFieldSuggestions = async (
    count: number,
    instructions: string,
  ) => {
    draftForAgent(`Suggest up to ${count} useful, typed, evidence-backed fields for the ${kind} table.${instructions.trim() ? ` They should help with: ${instructions.trim()}` : ""} Propose them for review before adding anything.`);
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
  const acceptRelationshipSuggestions = async (
    jobId: string,
    suggestionIds: string[],
  ) => {
    try {
      await post(`/api/research/jobs/${jobId}/relationships/accept`, {
        suggestion_ids: suggestionIds,
      });
      await loadOverview();
      await loadMatrix();
      setJobs(await api<ResearchJob[]>("/api/research/jobs"));
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Could not add relationship proposals",
      );
    }
  };
  const acceptProvisionalRelationships = async (
    suggestions: ProvisionalRelationship[],
    requestedScope?: "cell" | "column" | "table",
  ) => {
    if (!suggestions.length) return;
    const questionIds = new Set(
      suggestions.map((suggestion) => suggestion.question_id),
    );
    const subjectIds = new Set(
      suggestions.map((suggestion) => suggestion.subject_entity_id),
    );
    const scope =
      requestedScope ??
      (questionIds.size === 1 && subjectIds.size === 1
        ? "cell"
        : questionIds.size === 1
          ? "column"
          : "table");
    try {
      await post("/api/research/relationships/accept", {
        scope,
        entity_kind: scope === "table" ? kind : undefined,
        question_id:
          questionIds.size === 1 ? suggestions[0].question_id : undefined,
        subject_entity_id:
          subjectIds.size === 1 ? suggestions[0].subject_entity_id : undefined,
        reason: `Approved all provisional relationships in ${scope} review`,
      });
      await loadOverview();
      await loadMatrix();
      setJobs(await api<ResearchJob[]>("/api/research/jobs"));
      setClipboardNotice(
        `Accepted ${suggestions.length} provisional entr${suggestions.length === 1 ? "y" : "ies"}`,
      );
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Could not accept provisional entries",
      );
      throw caught;
    }
  };
  const rejectProvisionalRelationships = async (
    suggestions: ProvisionalRelationship[],
    scope: "cell" | "column" | "table",
  ) => {
    if (!suggestions.length) return;
    const reason = window.prompt(
      `Why are you rejecting ${suggestions.length} provisional entr${suggestions.length === 1 ? "y" : "ies"}?`,
    );
    if (!reason?.trim()) return;
    try {
      await post("/api/research/relationships/reject", {
        scope,
        entity_kind: scope === "table" ? kind : undefined,
        question_id: scope !== "table" ? suggestions[0].question_id : undefined,
        subject_entity_id:
          scope === "cell" ? suggestions[0].subject_entity_id : undefined,
        reason,
      });
      setJobs(await api<ResearchJob[]>("/api/research/jobs"));
      setClipboardNotice(
        `Rejected ${suggestions.length} provisional entr${suggestions.length === 1 ? "y" : "ies"}`,
      );
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Could not reject provisional entries",
      );
      throw caught;
    }
  };
  const acceptSchemaAdaptation = async (questionId: string) => {
    try {
      await post(`/api/research/schema-adaptations/${questionId}/accept`, {});
      await loadOverview();
      await loadMatrix();
      setJobs(await api<ResearchJob[]>("/api/research/jobs"));
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Could not apply schema adaptation",
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
    return [...questions]
      .filter((question) => !hiddenColumns.includes(question.name))
      .sort((left, right) => {
        const leftPosition = position.get(left.name);
        const rightPosition = position.get(right.name);
        if (leftPosition === undefined && rightPosition === undefined) return 0;
        if (leftPosition === undefined) return 1;
        if (rightPosition === undefined) return -1;
        return leftPosition - rightPosition;
      });
  }, [matrix, columnOrder, hiddenColumns]);
  const provisionalRelationships = useMemo(
    () =>
      jobs.flatMap((job) =>
        (job.relationship_suggestions ?? [])
          .filter((suggestion) => suggestion.status === "pending")
          .map((suggestion) => ({ ...suggestion, jobId: job.job_id })),
      ),
    [jobs],
  );
  const tableProvisionalRelationships = useMemo(() => {
    const questionIds = new Set(
      (matrix?.questions ?? []).map((question) => question.question_id),
    );
    return provisionalRelationships.filter((suggestion) =>
      questionIds.has(suggestion.question_id),
    );
  }, [matrix, provisionalRelationships]);
  const activeSelection = useMemo(() => {
    if (!activeGridCell || !matrix) return null;
    const row = matrix.rows.find(
      (candidate) => candidate.entity_id === activeGridCell.entityId,
    );
    const question = matrix.questions.find(
      (candidate) => candidate.question_id === activeGridCell.questionId,
    );
    if (!row || !question) return null;
    return { row, question, cell: row.cells[question.name] };
  }, [activeGridCell, matrix]);
  const tableWidth =
    56 +
    (columnWidths.__entity__ ?? 220) +
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
        (statusFilter === "provisional" &&
          tableProvisionalRelationships.some(
            (suggestion) => suggestion.subject_entity_id === row.entity_id,
          )) ||
        (statusFilter === "review" &&
          (cells.some(
            (cell) =>
              cell.state === "Contested" ||
              cell.state === "NotFound" ||
              cell.temporal?.freshness === "stale",
          ) ||
            tableProvisionalRelationships.some(
              (suggestion) => suggestion.subject_entity_id === row.entity_id,
            )));
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
  }, [matrix, filterText, statusFilter, sort, tableProvisionalRelationships]);
  const selectedRange = useMemo(() => {
    if (!activeGridCell || !selectionAnchor) return null;
    const anchorRow = displayedRows.findIndex(
      (row) => row.entity_id === selectionAnchor.entityId,
    );
    const activeRow = displayedRows.findIndex(
      (row) => row.entity_id === activeGridCell.entityId,
    );
    const anchorColumn = displayedQuestions.findIndex(
      (question) => question.question_id === selectionAnchor.questionId,
    );
    const activeColumn = displayedQuestions.findIndex(
      (question) => question.question_id === activeGridCell.questionId,
    );
    if (
      [anchorRow, activeRow, anchorColumn, activeColumn].some(
        (value) => value < 0,
      )
    )
      return null;
    return {
      firstRow: Math.min(anchorRow, activeRow),
      lastRow: Math.max(anchorRow, activeRow),
      firstColumn: Math.min(anchorColumn, activeColumn),
      lastColumn: Math.max(anchorColumn, activeColumn),
    };
  }, [activeGridCell, selectionAnchor, displayedRows, displayedQuestions]);
  const selectedCellCount = selectedRange
    ? (selectedRange.lastRow - selectedRange.firstRow + 1) *
      (selectedRange.lastColumn - selectedRange.firstColumn + 1)
    : 0;
  const activeCellAddress = activeSelection
    ? `${spreadsheetColumn(
        displayedQuestions.findIndex(
          (question) =>
            question.question_id === activeSelection.question.question_id,
        ) + 1,
      )}${
        displayedRows.findIndex(
          (row) => row.entity_id === activeSelection.row.entity_id,
        ) + 2
      }`
    : "—";
  const saveLayout = (
    nextWrapText: boolean,
    nextOrder: string[],
    nextWidths: Record<string, number>,
    nextSort: SortState = sort,
    nextStatusFilter: string = statusFilter,
    nextHiddenColumns: string[] = hiddenColumns,
    nextColumnFormats: Record<string, NumberFormat> = columnFormats,
  ) => {
    localStorage.setItem(
      layoutKey,
      JSON.stringify({
        wrapText: nextWrapText,
        columnOrder: nextOrder,
        columnWidths: nextWidths,
        sort: nextSort,
        statusFilter: nextStatusFilter,
        hiddenColumns: nextHiddenColumns,
        columnFormats: nextColumnFormats,
      }),
    );
  };
  const hideColumn = (name: string) => {
    const next = [...new Set([...hiddenColumns, name])];
    setHiddenColumns(next);
    setActiveViewId("");
    saveLayout(wrapText, columnOrder, columnWidths, sort, statusFilter, next);
  };
  const showColumn = (name: string) => {
    const next = hiddenColumns.filter((item) => item !== name);
    setHiddenColumns(next);
    setActiveViewId("");
    saveLayout(wrapText, columnOrder, columnWidths, sort, statusFilter, next);
  };
  const updateColumnFormat = (
    question: Question,
    update: Partial<NumberFormat>,
  ) => {
    const current = getColumnFormat(question);
    const next = {
      ...columnFormats,
      [question.name]: { ...current, ...update },
    };
    setColumnFormats(next);
    setActiveViewId("");
    saveLayout(
      wrapText,
      columnOrder,
      columnWidths,
      sort,
      statusFilter,
      hiddenColumns,
      next,
    );
  };
  const saveCurrentView = () => {
    const name = window.prompt("Name this view");
    if (!name?.trim()) return;
    const view: SavedView = {
      id: `view_${Date.now()}`,
      name: name.trim(),
      filterText,
      statusFilter,
      sort,
      wrapText,
      columnOrder,
      columnWidths,
      hiddenColumns,
      columnFormats,
    };
    const next = [...savedViews, view];
    setSavedViews(next);
    setActiveViewId(view.id);
    localStorage.setItem(viewsKey, JSON.stringify(next));
    setClipboardNotice(`Saved view “${view.name}”`);
  };
  const applyView = (id: string) => {
    setActiveViewId(id);
    if (!id) return;
    const view = savedViews.find((item) => item.id === id);
    if (!view) return;
    setFilterText(view.filterText);
    setStatusFilter(view.statusFilter);
    setSort(view.sort);
    setWrapText(view.wrapText);
    setColumnOrder(view.columnOrder);
    setColumnWidths(view.columnWidths);
    setHiddenColumns(view.hiddenColumns ?? []);
    setColumnFormats(view.columnFormats ?? {});
    saveLayout(
      view.wrapText,
      view.columnOrder,
      view.columnWidths,
      view.sort,
      view.statusFilter,
      view.hiddenColumns ?? [],
      view.columnFormats ?? {},
    );
  };
  const deleteActiveView = () => {
    if (!activeViewId) return;
    const view = savedViews.find((item) => item.id === activeViewId);
    if (!view || !window.confirm(`Delete saved view “${view.name}”?`)) return;
    const next = savedViews.filter((item) => item.id !== activeViewId);
    setSavedViews(next);
    setActiveViewId("");
    localStorage.setItem(viewsKey, JSON.stringify(next));
  };
  const toggleSort = (key: string) => {
    const next: SortState = {
      key,
      direction: sort.key === key && sort.direction === "asc" ? "desc" : "asc",
    };
    setSort(next);
    setActiveViewId("");
    saveLayout(wrapText, columnOrder, columnWidths, next);
  };
  const updateStatusFilter = (value: string) => {
    setStatusFilter(value);
    setActiveViewId("");
    saveLayout(wrapText, columnOrder, columnWidths, sort, value);
  };
  const toggleRows = () => {
    const next = !wrapText;
    setWrapText(next);
    setActiveViewId("");
    saveLayout(next, columnOrder, columnWidths);
  };
  const showRowView = () => {
    const entityId =
      activeGridCell?.entityId ??
      selection?.entityId ??
      entitySelection?.entityId ??
      displayedRows[0]?.entity_id;
    const row = matrix?.rows.find((item) => item.entity_id === entityId);
    if (!row) return;
    setEntitySelection({
      entityId: row.entity_id,
      entityName: row.name,
      entityKind: kind,
    });
    setCenterView("row");
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
      setActiveViewId("");
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
    setActiveViewId("");
    saveLayout(wrapText, names, columnWidths);
    setDraggedColumn(null);
  };
  const activateCell = (
    rowIndex: number,
    columnIndex: number,
    extendSelection = false,
  ) => {
    const row = displayedRows[rowIndex];
    const question = displayedQuestions[columnIndex];
    if (!row || !question) return;
    const next = {
      entityId: row.entity_id,
      questionId: question.question_id,
    };
    if (!extendSelection || !selectionAnchor) setSelectionAnchor(next);
    setActiveGridCell(next);
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
        const value = selectedRange
          ? displayedRows
              .slice(selectedRange.firstRow, selectedRange.lastRow + 1)
              .map((row) =>
                displayedQuestions
                  .slice(
                    selectedRange.firstColumn,
                    selectedRange.lastColumn + 1,
                  )
                  .map((question) =>
                    cellDisplay(
                      row.cells[question.name],
                      question,
                      columnFormats[question.name],
                    ),
                  )
                  .join("\t"),
              )
              .join("\n")
          : cellDisplay(
              cell,
              displayedQuestions[columnIndex],
              columnFormats[displayedQuestions[columnIndex]?.name],
            );
        await navigator.clipboard.writeText(value);
        setClipboardNotice(
          selectedCellCount > 1
            ? `Copied ${selectedCellCount} cells as tab-separated values`
            : "Copied cell value",
        );
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
      setSelectionAnchor(null);
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
      event.shiftKey,
    );
  };
  const preparePaste = (
    event: React.ClipboardEvent<HTMLTableCellElement>,
    startRow: number,
    startColumn: number,
  ) => {
    const text = event.clipboardData.getData("text/plain");
    if (!text.trim()) return;
    event.preventDefault();
    const grid = text
      .replace(/\r\n?/g, "\n")
      .replace(/\n$/, "")
      .split("\n")
      .map((line) => line.split("\t"));
    const claims: PastedClaim[] = [];
    grid.forEach((values, rowOffset) => {
      values.forEach((rawValue, columnOffset) => {
        const row = displayedRows[startRow + rowOffset];
        const question = displayedQuestions[startColumn + columnOffset];
        if (!row || !question || !rawValue.trim()) return;
        try {
          claims.push({
            entityId: row.entity_id,
            entityName: row.name,
            question,
            rawValue,
            existingState: row.cells[question.name].state,
            value: parseValue(rawValue, question.value_type),
          });
        } catch (caught) {
          claims.push({
            entityId: row.entity_id,
            entityName: row.name,
            question,
            rawValue,
            existingState: row.cells[question.name].state,
            error: caught instanceof Error ? caught.message : "Invalid value",
          });
        }
      });
    });
    if (!claims.length) {
      setError("The pasted range did not contain values within the table");
      return;
    }
    setPastedClaims(claims);
    setDialog("paste");
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

  return (
    <div className="app-shell">
      <header>
        <div className="brand">
          <span className="mark" aria-label="Epiq">
            E[🦜]
          </span>
          <div>
            <b>Epiq</b>
            <small>Evidence-backed workspace</small>
          </div>
        </div>
        <div className="project-title">
          <button onClick={() => setShowProjects(true)}>
            <span>{overview?.project.name ?? "Untitled project"}</span>
            {kind && <small>/ {kind}</small>}
            <i>⌄</i>
          </button>
        </div>
        <div className="header-actions">
          <button className="agent-command-button" onClick={() => setRightPane("agent")}>
            <span>✦ Control Center</span>
          </button>
          <button className="ghost" onClick={() => setRightPane("activity")}>
            <span>✦ Activity</span>
            {jobs.filter(
              (job) => job.status === "queued" || job.status === "running",
            ).length ? (
              <strong className="action-count active">
                {
                  jobs.filter(
                    (job) =>
                      job.status === "queued" || job.status === "running",
                  ).length
                }
              </strong>
            ) : null}
          </button>
          <button className="ghost" onClick={() => setRightPane("review")}>
            <span>◈ Review</span>
            {reviewItems.stale.length +
            reviewItems.contradictions.length +
            staleDerivations.length +
            tableProvisionalRelationships.length ? (
              <strong className="action-count">
                {reviewItems.stale.length +
                  reviewItems.contradictions.length +
                  staleDerivations.length +
                  tableProvisionalRelationships.length}
              </strong>
            ) : (
              ""
            )}
          </button>
          <details className="export-menu">
            <summary>↓ Export</summary>
            <div>
              {kind && (
                <>
                  <b>Current {kind} table</b>
                  <a href={`/api/export/${encodeURIComponent(kind)}.xlsx`} download>
                    <span>Excel audit workbook</span>
                    <small>Table, evidence, gaps, schema, and event log</small>
                  </a>
                  <a
                    href={`/api/export/${encodeURIComponent(kind)}.scenario-list.ep`}
                    download
                  >
                    <span>EDSL ScenarioList</span>
                    <small>Native Git-backed .ep package</small>
                  </a>
                  <a
                    href={`/api/export/${encodeURIComponent(kind)}.agent-list.ep`}
                    download
                  >
                    <span>EDSL AgentList</span>
                    <small>Rows become named agents with typed traits</small>
                  </a>
                </>
              )}
              <b>Whole project</b>
              <a href="/api/export/project.sqlite" download>
                <span>SQLite database</span>
                <small>Transactionally consistent native database</small>
              </a>
              <a href="/api/export/project.epiq" download>
                <span>Epiq project bundle</span>
                <small>Portable checksummed archive</small>
              </a>
            </div>
          </details>
          <details className="export-menu workspace-menu">
            <summary aria-label="Workspace menu">•••</summary>
            <div>
              <b>Workspace</b>
              <button onClick={() => setShowSchemaReview(true)}>
                <span>Schema review</span>
                <small>
                  {questionChallenges.length || "No"} open challenge
                  {questionChallenges.length === 1 ? "" : "s"}
                </small>
              </button>
              <button onClick={() => void refresh()}>
                <span>Refresh workspace</span>
                <small>Reload current data and diagnostics</small>
              </button>
              <button onClick={() => setShowProjects(true)}>
                <span>Open another project</span>
                <small>Browse local Epiq databases</small>
              </button>
              <button
                className="destructive-menu-action"
                onClick={() => void closeProject()}
              >
                <span>Close project</span>
                <small>Return to the project browser</small>
              </button>
            </div>
          </details>
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
            Git ledger connected
          </div>
        </aside>
        <main>
          {!kind ? (
            <div className="blank-workspace">
              <div className="blank-workspace-actions">
                <button
                  className="primary"
                  onClick={() => setRightPane("agent")}
                >
                  ✦ Control Center
                </button>
              </div>
            </div>
          ) : (
            <>
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
            <button className="ghost" onClick={() => setRightPane("agent")}>
              ✦ Request a change
            </button>
          </div>
          <div
            className="sheet-format-toolbar"
            aria-label="Spreadsheet formatting"
          >
            <select
              aria-label="Number format"
              disabled={
                !activeSelection ||
                !["Float", "Probability"].includes(
                  activeSelection.question.value_type,
                )
              }
              value={
                activeSelection
                  ? getColumnFormat(activeSelection.question).precision
                  : "significant"
              }
              onChange={(event) =>
                activeSelection &&
                updateColumnFormat(activeSelection.question, {
                  precision: event.target.value as "decimal" | "significant",
                })
              }
            >
              <option value="significant">123 · significant</option>
              <option value="decimal">0.00 · decimal</option>
            </select>
            <button
              className={
                activeSelection &&
                ["Int", "Float", "Probability"].includes(
                  activeSelection.question.value_type,
                ) &&
                getColumnFormat(activeSelection.question).use_grouping
                  ? "toolbar-icon active"
                  : "toolbar-icon"
              }
              title="Thousands separator"
              disabled={
                !activeSelection ||
                !["Int", "Float", "Probability"].includes(
                  activeSelection.question.value_type,
                )
              }
              onClick={() =>
                activeSelection &&
                updateColumnFormat(activeSelection.question, {
                  use_grouping: !getColumnFormat(activeSelection.question)
                    .use_grouping,
                })
              }
            >
              ,
            </button>
            <button
              className="toolbar-icon"
              title="Decrease decimal places or significant digits"
              disabled={
                !activeSelection ||
                !["Float", "Probability"].includes(
                  activeSelection.question.value_type,
                )
              }
              onClick={() =>
                activeSelection &&
                updateColumnFormat(activeSelection.question, {
                  digits: Math.max(
                    1,
                    getColumnFormat(activeSelection.question).digits - 1,
                  ),
                })
              }
            >
              .0←
            </button>
            <button
              className="toolbar-icon"
              title="Increase decimal places or significant digits"
              disabled={
                !activeSelection ||
                !["Float", "Probability"].includes(
                  activeSelection.question.value_type,
                )
              }
              onClick={() =>
                activeSelection &&
                updateColumnFormat(activeSelection.question, {
                  digits: Math.min(
                    12,
                    getColumnFormat(activeSelection.question).digits + 1,
                  ),
                })
              }
            >
              .00→
            </button>
            <span className="toolbar-divider" />
            <button
              className={wrapText ? "toolbar-text active" : "toolbar-text"}
              onClick={toggleRows}
              title="Toggle text wrapping"
            >
              ↵ Wrap
            </button>
            <span className="toolbar-selection-hint">
              {activeSelection
                ? `${String(activeSelection.question.definition.label ?? activeSelection.question.name)} · ${activeSelection.question.value_type}`
                : "Select a cell to format"}
            </span>
          </div>
          <div className="view-toolbar" aria-label="Table view controls">
            <label className="table-search">
              <span>⌕</span>
              <input
                type="search"
                value={filterText}
                onChange={(event) => {
                  setFilterText(event.target.value);
                  setActiveViewId("");
                }}
                placeholder={`Filter ${kind.toLowerCase()} rows or values…`}
                aria-label="Filter rows"
              />
            </label>
            <select
              className="status-filter-select"
              value={statusFilter}
              onChange={(event) => updateStatusFilter(event.target.value)}
              aria-label="Filter by research status"
            >
              <option value="all">All rows</option>
              <option value="answered">Has answers</option>
              <option value="unanswered">Has unanswered fields</option>
              <option value="provisional">Has provisional entries</option>
              <option value="review">Needs review</option>
            </select>
            <select
              className="saved-view-select"
              value={activeViewId}
              onChange={(event) => applyView(event.target.value)}
              aria-label="Saved view"
            >
              <option value="">Current view</option>
              {savedViews.map((view) => (
                <option key={view.id} value={view.id}>
                  {view.name}
                </option>
              ))}
            </select>
            <button className="view-action-button" onClick={saveCurrentView}>
              Save
            </button>
            {activeViewId && (
              <button className="view-action-button" onClick={deleteActiveView}>
                Delete view
              </button>
            )}
            <details className="field-visibility-menu">
              <summary>
                Fields
                {hiddenColumns.length
                  ? ` · ${hiddenColumns.length} hidden`
                  : ""}
              </summary>
              <div>
                <b>Visible fields</b>
                {(matrix?.questions ?? []).map((question) => {
                  const hidden = hiddenColumns.includes(question.name);
                  return (
                    <label key={question.question_id}>
                      <input
                        type="checkbox"
                        checked={!hidden}
                        onChange={() =>
                          hidden
                            ? showColumn(question.name)
                            : hideColumn(question.name)
                        }
                      />
                      <span>
                        {String(question.definition.label ?? question.name)}
                      </span>
                    </label>
                  );
                })}
              </div>
            </details>
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
              Arrows move · Enter inspects · ⌘/Ctrl+C copies · ⌘/Ctrl+V pastes
            </span>
          </div>
          {activeSelection && (
            <div
              className="selection-bar formula-bar"
              aria-label="Current cell selection"
            >
              <code className="cell-address">{activeCellAddress}</code>
              <span className="formula-symbol">fx</span>
              {selectedCellCount > 1 && (
                <code>{selectedCellCount} cells selected</code>
              )}
              <span className="selection-value">
                {cellDisplay(
                  activeSelection.cell,
                  activeSelection.question,
                  columnFormats[activeSelection.question.name],
                ) || "No value"}
              </span>
              <span
                className={`selection-state state-${activeSelection.cell.state.toLowerCase()}`}
              />
              <code>{activeSelection.cell.state}</code>
              <button
                onClick={() => {
                  setSelection({
                    entityId: activeSelection.row.entity_id,
                    entityName: activeSelection.row.name,
                    question: activeSelection.question,
                    cell: activeSelection.cell,
                  });
                  setRightPane("inspect");
                }}
              >
                Inspect
              </button>
              {selectedRange &&
                selectedRange.firstColumn === selectedRange.lastColumn &&
                selectedCellCount > 1 &&
                !activeSelection.question.definition.formula && (
                  <button onClick={() => setDialog("fill")}>
                    Fill selection
                  </button>
                )}
            </div>
          )}
          <div className="center-view-switch" aria-label="Data view">
            <button
              className={centerView === "table" ? "active" : ""}
              onClick={() => setCenterView("table")}
            >
              Table
            </button>
            <button
              className={centerView === "row" ? "active" : ""}
              disabled={!matrix?.rows.length}
              onClick={showRowView}
            >
              Row
            </button>
          </div>
          {centerView === "row" && entitySelection && matrix?.rows.some(
            (row) => row.entity_id === entitySelection.entityId,
          ) ? (
            <CenterRowView
              row={matrix.rows.find((row) => row.entity_id === entitySelection.entityId)!}
              questions={matrix.questions}
              onInspect={(question, cell) => {
                setSelection({
                  entityId: entitySelection.entityId,
                  entityName: entitySelection.entityName,
                  question,
                  cell,
                });
                setRightPane("inspect");
              }}
            />
          ) : (
          <div className="grid-wrap">
            <table
              className={`grid ${wrapText ? "wrap-text" : "compact-rows"}`}
              style={{ width: tableWidth }}
            >
              <colgroup>
                <col style={{ width: 56 }} />
                <col style={{ width: columnWidths.__entity__ ?? 220 }} />
                {displayedQuestions.map((question) => (
                  <col
                    key={question.name}
                    style={{ width: columnWidths[question.name] ?? 180 }}
                  />
                ))}
              </colgroup>
              <thead>
                <tr className="field-header-row">
                  <th className="row-action-heading">#</th>
                  <th className="name-column entity-column-head">
                    <em className="spreadsheet-column-letter">A</em>
                    <div className="entity-column-label column-header-title">
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
                  {displayedQuestions.map((question, questionIndex) => {
                    const pendingProvisional =
                      tableProvisionalRelationships.filter(
                        (suggestion) =>
                          suggestion.question_name === question.name,
                      );
                    const activeColumnJobs = activeResearchJobs.filter(
                      (job) => job.question_id === question.question_id,
                    );
                    return (
                      <th
                        key={question.question_id}
                        className={`reorderable-column ${question.schema_state === "challenged" ? "column-challenged" : ""}`}
                        draggable
                        onDragStart={(event) => {
                          if (
                            (event.target as HTMLElement).closest(
                              ".formula-fill-button",
                            )
                          )
                            return;
                          if (
                            (event.target as HTMLElement).closest(
                              "button,summary,.column-resizer",
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
                        <em className="spreadsheet-column-letter">
                          {spreadsheetColumn(questionIndex + 1)}
                        </em>
                        <div className="column-header-title">
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
                        </div>
                        <div className="column-actions">
                          <details className="column-menu">
                            <summary title="Field actions">•••</summary>
                            <div>
                              {Boolean(question.definition.formula) && (
                                <button
                                  onClick={() =>
                                    void materializeFormula(question)
                                  }
                                >
                                  Calculate formula
                                </button>
                              )}
                              <button onClick={() => toggleSort(question.name)}>
                                Sort{" "}
                                {sort.key === question.name &&
                                sort.direction === "asc"
                                  ? "descending"
                                  : "ascending"}
                              </button>
                              <button
                                onClick={() => {
                                  setSchemaChallengeQuestion(question);
                                  setDialog("schemaChallenge");
                                }}
                              >
                                Challenge schema
                              </button>
                              {pendingProvisional.length > 0 && (
                                <>
                                  <button
                                    onClick={() =>
                                      void acceptProvisionalRelationships(
                                        pendingProvisional,
                                        "column",
                                      )
                                    }
                                  >
                                    Accept all {pendingProvisional.length}{" "}
                                    provisional
                                  </button>
                                  <button
                                    onClick={() =>
                                      void rejectProvisionalRelationships(
                                        pendingProvisional,
                                        "column",
                                      )
                                    }
                                  >
                                    Reject all provisional
                                  </button>
                                </>
                              )}
                              {activeColumnJobs.length > 0 && (
                                <button
                                  className="destructive-menu-action"
                                  onClick={() =>
                                    void cancelResearchScope(
                                      "column",
                                      undefined,
                                      question.question_id,
                                    )
                                  }
                                >
                                  Stop column research (
                                  {activeColumnJobs.length})
                                </button>
                              )}
                              <button onClick={() => hideColumn(question.name)}>
                                Hide field
                              </button>
                            </div>
                          </details>
                        </div>
                        <span
                          className="column-resizer"
                          onMouseDown={(event) =>
                            resizeColumn(question.name, event)
                          }
                        />
                      </th>
                    );
                  })}
                </tr>
              </thead>
              <tbody>
                {displayedRows.map((row, index) => {
                  const isResearching = activeRowEntityIds.has(row.entity_id);
                  const rowProvisionalCount =
                    tableProvisionalRelationships.filter(
                      (suggestion) =>
                        suggestion.subject_entity_id === row.entity_id,
                    ).length;
                  return (
                    <tr
                      key={row.entity_id}
                      className={isResearching ? "row-is-researching" : ""}
                    >
                      <td className="row-action-cell">
                        <small className="row-index">{index + 1}</small>
                        {isResearching && <span className="row-spinner" />}
                      </td>
                      <td
                        className="entity-name"
                        title="Open row details"
                        onClick={() => {
                          setSelection(null);
                          setEntitySelection({
                            entityId: row.entity_id,
                            entityName: row.name,
                            entityKind: kind,
                          });
                          setCenterView("row");
                          setRightPane("inspect");
                        }}
                      >
                        <div className="entity-inner">
                          <span>{row.name}</span>
                          <small className="backref-affordance">↩ refs</small>
                        </div>
                      </td>
                      {displayedQuestions.map((question) => {
                        const columnIndex =
                          displayedQuestions.indexOf(question);
                        const cell = row.cells[question.name];
                        const provisional =
                          tableProvisionalRelationships.filter(
                            (suggestion) =>
                              suggestion.subject_entity_id === row.entity_id &&
                              suggestion.question_name === question.name,
                          );
                        const derivedClaims = cell.lineage.filter(
                          (item) => item.derivation,
                        );
                        const derivedIsStale = derivedClaims.some((item) =>
                          staleDerivedClaimIds.has(item.claim_id),
                        );
                        const isInSelectedRange = Boolean(
                          selectedRange &&
                          index >= selectedRange.firstRow &&
                          index <= selectedRange.lastRow &&
                          columnIndex >= selectedRange.firstColumn &&
                          columnIndex <= selectedRange.lastColumn,
                        );
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
                            className={`data-cell type-${question.value_type.toLowerCase()} state-${cell.state.toLowerCase()} ${provisional.length ? "has-provisional" : ""} ${derivedClaims.length ? "derived-cell" : ""} ${derivedIsStale ? "derived-cell-stale" : ""} ${isCellResearching(row.entity_id, question.question_id) ? "cell-is-researching" : ""} ${formulaDragQuestion?.question_id === question.question_id ? "formula-drop-target" : ""} ${isInSelectedRange ? "selected-cell" : ""} ${activeGridCell?.entityId === row.entity_id && activeGridCell?.questionId === question.question_id ? "active-cell" : ""}`}
                            onDragOver={(event) => {
                              if (
                                formulaDragQuestion?.question_id ===
                                question.question_id
                              ) {
                                event.preventDefault();
                                event.dataTransfer.dropEffect = "copy";
                              }
                            }}
                            onDrop={(event) => {
                              if (
                                formulaDragQuestion?.question_id !==
                                question.question_id
                              )
                                return;
                              event.preventDefault();
                              const subjects = displayedRows
                                .slice(0, index + 1)
                                .map((item) => item.name);
                              setFormulaDragQuestion(null);
                              void materializeFormula(question, subjects);
                            }}
                            onFocus={() => {
                              const focused = {
                                entityId: row.entity_id,
                                questionId: question.question_id,
                              };
                              setActiveGridCell(focused);
                              setSelectionAnchor(
                                (current) => current ?? focused,
                              );
                            }}
                            onKeyDown={(event) =>
                              void handleCellKey(
                                event,
                                index,
                                columnIndex,
                                cell,
                              )
                            }
                            onPaste={(event) =>
                              preparePaste(event, index, columnIndex)
                            }
                            onClick={(event) => {
                              const clicked = {
                                entityId: row.entity_id,
                                questionId: question.question_id,
                              };
                              if (!event.shiftKey || !selectionAnchor)
                                setSelectionAnchor(clicked);
                              setActiveGridCell(clicked);
                              setEntitySelection(null);
                              setSelection({
                                entityId: row.entity_id,
                                entityName: row.name,
                                question,
                                cell,
                              });
                              setRightPane("inspect");
                            }}
                          >
                            <div className="cell-value">
                              {isCellResearching(
                                row.entity_id,
                                question.question_id,
                              ) && <span className="cell-spinner" />}
                              {cell.references?.length ? (
                                <div className="reference-cell">
                                  <strong>
                                    {cell.references
                                      .map((reference) => reference.name)
                                      .join(", ")}
                                  </strong>
                                  <small>
                                    ↗ {cell.references[0].kind} ·{" "}
                                    {cell.references
                                      .map((reference) => reference.entity_id)
                                      .join(", ")}
                                  </small>
                                </div>
                              ) : question.value_type === "Image" &&
                              cell.state === "Answered" &&
                              typeof cell.value === "string" ? (
                                <img
                                  className="cell-thumbnail"
                                  src={cell.value}
                                  alt={`${row.name} · ${question.definition.label ?? question.name}`}
                                  title={cell.value}
                                  loading="lazy"
                                  referrerPolicy="no-referrer"
                                  onClick={(event) => event.stopPropagation()}
                                  onDoubleClick={(event) =>
                                    event.stopPropagation()
                                  }
                                />
                              ) : question.value_type === "URL" &&
                              cell.state === "Answered" &&
                              typeof cell.value === "string" ? (
                                <a
                                  className="cell-url"
                                  href={cell.value}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  title={cell.value}
                                  onClick={(event) => event.stopPropagation()}
                                  onDoubleClick={(event) =>
                                    event.stopPropagation()
                                  }
                                >
                                  {cell.value} ↗
                                </a>
                              ) : (
                                cellDisplay(
                                  cell,
                                  question,
                                  columnFormats[question.name],
                                )
                              )}
                            </div>
                            {provisional.length > 0 && (
                              <div className="provisional-cell-values">
                                {provisional.map((suggestion) => (
                                  <span key={suggestion.suggestion_id}>
                                    {suggestion.target_name}
                                  </span>
                                ))}
                                <em>provisional</em>
                              </div>
                            )}
                            {cell.state !== "Unasked" && (
                              <span className="state-dot" title={cell.state} />
                            )}
                            {cell.lineage.length > 0 && (
                              <span className="source-count">
                                {cell.lineage.length} src
                              </span>
                            )}
                            {derivedClaims.length > 0 && (
                              <span
                                className="derivation-badge"
                                title={
                                  derivedIsStale
                                    ? "Derived value has changed dependencies"
                                    : `Derived with ${derivedClaims[0].derivation?.operation}`
                                }
                              >
                                {derivedIsStale ? "⚠ ƒ derived" : "ƒ derived"}
                              </span>
                            )}
                          </td>
                        );
                      })}
                    </tr>
                  );
                })}
                {matrix?.rows.length === 0 && (
                  <tr className="empty-table-row">
                    <td colSpan={(matrix?.questions.length ?? 0) + 2}>
                      <div className="empty-table-state">
                        <div className="empty-table-icon">▦</div>
                        <h2>Your research table is empty</h2>
                        <p>
                          Use the Control Center to describe the rows and fields
                          this table should contain.
                        </p>
                        <button
                          className="primary"
                          onClick={() => setRightPane("agent")}
                        >
                          Open Control Center
                        </button>
                      </div>
                    </td>
                  </tr>
                )}
                {(matrix?.rows.length ?? 0) > 0 &&
                  displayedRows.length === 0 && (
                    <tr className="empty-table-row filtered-table-row">
                      <td colSpan={(matrix?.questions.length ?? 0) + 2}>
                        <div className="empty-table-state">
                          <div className="empty-table-icon">⌕</div>
                          <h2>No rows match this view</h2>
                          <p>
                            Try a different search or clear the status filter.
                          </p>
                          <button
                            onClick={() => {
                              setFilterText("");
                              updateStatusFilter("all");
                            }}
                          >
                            Clear filters
                          </button>
                        </div>
                      </td>
                    </tr>
                  )}
              </tbody>
            </table>
          </div>
          )}
          <div className="toast-region" aria-live="polite" aria-atomic="true">
            {error && (
              <div className="app-toast error" role="alert">
                <span>!</span>
                <p>{error}</p>
                <button aria-label="Dismiss error" onClick={() => setError("")}>
                  ×
                </button>
              </div>
            )}
          </div>
            </>
          )}
        </main>
        {rightPane && (
          <aside className="workspace-pane">
            <nav className="workspace-pane-tabs">
              {(["agent", "inspect", "activity", "review"] as const).map((pane) => (
                <button
                  key={pane}
                  className={rightPane === pane ? "active" : ""}
                  onClick={() => setRightPane(pane)}
                >
                  {pane === "agent" ? "Control Center" : pane[0].toUpperCase() + pane.slice(1)}
                </button>
              ))}
              <button className="workspace-pane-collapse" onClick={() => setRightPane(null)}>×</button>
            </nav>
            <div className="workspace-pane-content">
        {rightPane === "inspect" && selection && (
          <CellDrawer
            selection={selection}
            provisionalRelationships={tableProvisionalRelationships.filter(
              (suggestion) =>
                suggestion.subject_entity_id === selection.entityId &&
                suggestion.question_name === selection.question.name,
            )}
            staleDerivedClaimIds={staleDerivedClaimIds}
            isResearching={isCellResearching(
              selection.entityId,
              selection.question.question_id,
            )}
            onClose={() => setRightPane(null)}
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
            onCancelResearch={() =>
              cancelResearchScope(
                "cell",
                selection.entityId,
                selection.question.question_id,
              )
            }
            onPolicy={() => setDialog("policy")}
            onSchemaChallenge={() => {
              setSchemaChallengeQuestion(selection.question);
              setDialog("schemaChallenge");
            }}
            onChallengeResearch={() => {
              const label = String(
                selection.question.definition.label ?? selection.question.name,
              );
              const prior = selection.cell.research;
              const context = [
                `Revisit the NotFound result for ${selection.entityName} · ${label}.`,
                prior?.query ? `Previous query: ${prior.query}` : "",
                prior?.notes ? `Previous result: ${prior.notes}` : "",
                "Explain what prevented an answer, then retry with a better evidence strategy. Preserve existing data and add a value only when supported by appropriate evidence.",
              ].filter(Boolean).join("\n\n");
              draftForAgent(context);
            }}
            onEstimate={() => {
              const label = String(
                selection.question.definition.label ?? selection.question.name,
              );
              draftForAgent(
                `Provide a useful ballpark estimate for ${selection.entityName} · ${label}.\n\n` +
                "An exact answer was not found. Use an authoritative reference class or proxy, return a point estimate and plausible range, and state the method, proxy, and assumptions. Mark the result as estimated so it cannot be mistaken for an exact fact.",
              );
            }}
            onChallenge={(claimId) => {
              const label = String(
                selection.question.definition.label ?? selection.question.name,
              );
              const claimEvidence = selection.cell.lineage.filter(
                (item) => item.claim_id === claimId,
              );
              const currentValue = claimEvidence[0]?.value ?? selection.cell.value;
              const sources = claimEvidence.slice(0, 3).map(
                (item) =>
                  `Source: ${item.source.title}${item.source.url ? ` (${item.source.url})` : ""}\nExcerpt: ${item.excerpt}`,
              );
              const context = [
                `Revisit the current answer for ${selection.entityName} · ${label}.`,
                `Current value: ${JSON.stringify(currentValue)}\nClaim ID: ${claimId}`,
                ...sources,
                "Help me assess what may be wrong with this answer or its interpretation. Propose explicit Epiq CLI commands to correct or retract it and research a replacement if appropriate. Do not change anything without preserving the evidence trail.",
              ].filter(Boolean).join("\n\n");
              draftForAgent(context);
            }}
            onAcceptProvisional={async (suggestion) => {
              await acceptRelationshipSuggestions(suggestion.jobId, [
                suggestion.suggestion_id,
              ]);
            }}
            onAcceptAllProvisional={(suggestions) =>
              acceptProvisionalRelationships(suggestions, "cell")
            }
            onRejectAllProvisional={(suggestions) =>
              rejectProvisionalRelationships(suggestions, "cell")
            }
            onChanged={refresh}
            onReviewed={async () => {
              await loadMatrix(false);
              await loadReviewItems();
            }}
          />
        )}
        {rightPane === "inspect" && !selection && entitySelection && matrix && (
          <EntityRelationshipsDrawer
            selection={entitySelection}
            row={matrix.rows.find(
              (row) => row.entity_id === entitySelection.entityId,
            )!}
            questions={matrix.questions}
            showFields={centerView !== "row"}
            onClose={() => setRightPane(null)}
            onMerge={() => setDialog("mergeEntity")}
            onInspect={(question, cell) => {
              setSelection({
                entityId: entitySelection.entityId,
                entityName: entitySelection.entityName,
                question,
                cell,
              });
            }}
            onNavigate={(entity) => {
              setEntitySelection(null);
              setKind(entity.kind);
              setFilterText(entity.name);
            }}
          />
        )}
        {rightPane === "inspect" && !selection && !entitySelection && (
          <div className="workspace-pane-empty"><b>Select a row or cell</b><p>Its values, evidence, and relationships will appear here.</p></div>
        )}
        {rightPane === "activity" && (
          <ActivityPanel
            jobs={jobs}
            onClose={() => setRightPane(null)}
            onSuggestion={updateSuggestion}
            onAcceptFields={acceptFieldSuggestions}
            onAcceptRelationships={acceptRelationshipSuggestions}
            onAcceptSchemaAdaptation={acceptSchemaAdaptation}
            onCancel={cancelResearch}
            onRetry={retryResearch}
          />
        )}
        {rightPane === "agent" && (
          <WorkspaceAgentPanel
            jobs={jobs}
            curator={curator}
            draft={chatDraft}
            onDraftConsumed={() => setChatDraft("")}
            onClose={() => setRightPane(null)}
            onSend={directWorkspaceAgent}
            onCancel={cancelResearch}
            onApprove={approveWorkspacePlan}
            onReject={rejectWorkspacePlan}
            onStartCurator={startCurator}
            onControlCurator={controlCurator}
          />
        )}
        {rightPane === "review" && (
          <ReviewPanel
            stale={reviewItems.stale}
            contradictions={reviewItems.contradictions}
            staleDerivations={staleDerivations}
            provisionalRelationships={tableProvisionalRelationships}
            onClose={() => setRightPane(null)}
            onInspect={inspectDiagnostic}
            onRecalculate={async (item) => {
              const question = matrix?.questions.find(
                (candidate) => candidate.name === item.question,
              );
              if (question) await materializeFormula(question);
              await loadReviewItems();
            }}
            onInspectProvisional={(suggestion) => {
              const row = matrix?.rows.find(
                (item) => item.entity_id === suggestion.subject_entity_id,
              );
              const question = matrix?.questions.find(
                (item) => item.question_id === suggestion.question_id,
              );
              if (!row || !question) return;
              setSelection({
                entityId: row.entity_id,
                entityName: row.name,
                question,
                cell: row.cells[question.name],
              });
              setRightPane("inspect");
            }}
            onAcceptProvisional={(suggestions) =>
              acceptProvisionalRelationships(suggestions, "cell")
            }
            onAcceptAllProvisional={() =>
              acceptProvisionalRelationships(
                tableProvisionalRelationships,
                "table",
              )
            }
            onRejectProvisional={(suggestions) =>
              rejectProvisionalRelationships(suggestions, "cell")
            }
            onRejectAllProvisional={() =>
              rejectProvisionalRelationships(
                tableProvisionalRelationships,
                "table",
              )
            }
          />
        )}
            </div>
          </aside>
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
      {dialog === "entityKind" && (
        <EntityKindDialog
          onClose={() => setDialog(null)}
          onSaved={async (createdKind) => {
            setDialog(null);
            await loadOverview();
            setKind(createdKind);
          }}
        />
      )}
      {dialog === "question" && (
        <QuestionDialog
          kind={kind}
          questions={displayedQuestions}
          entityKinds={kinds.map((item) => item.kind)}
          onClose={() => setDialog(null)}
          onSaved={refresh}
        />
      )}
      {dialog === "editQuestion" && editQuestion && (
        <EditQuestionDialog
          question={editQuestion}
          questions={displayedQuestions}
          onClose={() => {
            setDialog(null);
            setEditQuestion(null);
          }}
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
      {dialog === "mergeEntity" && entitySelection && matrix && (
        <MergeEntityDialog
          source={entitySelection}
          rows={matrix.rows}
          questions={matrix.questions}
          onClose={() => setDialog(null)}
          onSaved={async () => {
            setDialog(null);
            setEntitySelection(null);
            setSelection(null);
            setActiveGridCell(null);
            await loadOverview();
            await loadMatrix();
          }}
        />
      )}
      {dialog === "findDuplicates" && (
        <DuplicateReviewDialog
          kind={kind}
          onClose={() => setDialog(null)}
          onChanged={async () => {
            await loadOverview();
            await loadMatrix();
          }}
        />
      )}
      {dialog === "paste" && pastedClaims.length > 0 && (
        <PasteDialog
          claims={pastedClaims}
          onClose={() => {
            setDialog(null);
            setPastedClaims([]);
          }}
          onSaved={async () => {
            setDialog(null);
            setPastedClaims([]);
            setClipboardNotice(`Added ${pastedClaims.length} sourced values`);
            await refresh();
          }}
        />
      )}
      {dialog === "fill" && selectedRange && activeSelection && (
        <FillSelectionDialog
          rows={displayedRows.slice(
            selectedRange.firstRow,
            selectedRange.lastRow + 1,
          )}
          question={activeSelection.question}
          onClose={() => setDialog(null)}
          onContinue={(claims) => {
            setPastedClaims(claims);
            setDialog("paste");
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
  const [importing, setImporting] = useState(false);
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
  const importProject = async (file: File | undefined) => {
    if (!file) return;
    setImporting(true);
    setError("");
    try {
      await api<ProjectInfo>(
        `/api/projects/import?filename=${encodeURIComponent(file.name)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/vnd.sqlite3" },
          body: file,
        },
      );
      await onReady();
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not import project",
      );
    } finally {
      setImporting(false);
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
      <div className="import-project">
        <div>
          <b>Import an Epiq project</b>
          <small>
            Upload a portable SQLite database. Epiq opens a managed copy; your
            original file is unchanged.
          </small>
        </div>
        <label className={importing ? "button-like disabled" : "button-like"}>
          {importing ? "Importing…" : "Choose SQLite file"}
          <input
            type="file"
            accept=".sqlite,.sqlite3,.db,application/vnd.sqlite3"
            disabled={importing}
            onChange={(event) => {
              void importProject(event.target.files?.[0]);
              event.currentTarget.value = "";
            }}
          />
        </label>
      </div>
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

function EntityKindDialog({
  onClose,
  onSaved,
}: {
  onClose: () => void;
  onSaved: (kind: string) => Promise<void>;
}) {
  const [kind, setKind] = useState("");
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await post("/api/entity-kinds", { kind: kind.trim() });
      await onSaved(kind.trim());
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not add table",
      );
    }
  };
  return (
    <Modal
      title="Add table"
      subtitle="A table defines a reusable entity type. Relationships can link rows across tables."
      onClose={onClose}
    >
      <form onSubmit={(event) => void submit(event)}>
        <label>
          Entity type
          <input
            value={kind}
            onChange={(event) => setKind(event.target.value)}
            placeholder="Author"
            autoFocus
            required
          />
          <span className="field-hint">
            Use a singular name such as Author, Company, or Paper.
          </span>
        </label>
        {error && <div className="form-error">{error}</div>}
        <div className="modal-actions">
          <button type="button" className="ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="primary">Add table</button>
        </div>
      </form>
    </Modal>
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
  questions,
  entityKinds,
  onClose,
  onSaved,
}: {
  kind: string;
  questions: Question[];
  entityKinds: string[];
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [label, setLabel] = useState("");
  const [type, setType] = useState("String");
  const [relationshipTarget, setRelationshipTarget] = useState("");
  const [enumChoices, setEnumChoices] = useState("");
  const [many, setMany] = useState(false);
  const [volatility, setVolatility] = useState("stable");
  const [computed, setComputed] = useState(false);
  const [formulaOperation, setFormulaOperation] = useState("sum");
  const [formulaInputs, setFormulaInputs] = useState<string[]>([]);
  const [formulaExpression, setFormulaExpression] = useState("");
  const [useGrouping, setUseGrouping] = useState(true);
  const [precision, setPrecision] = useState("significant");
  const [formatDigits, setFormatDigits] = useState("3");
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
      let formula = null;
      if (computed && formulaExpression.trim()) {
        try {
          formula = parseSpreadsheetFormula(formulaExpression, questions);
        } catch (caught) {
          setError(
            caught instanceof Error ? caught.message : "Invalid formula",
          );
          return;
        }
      } else if (computed) {
        if (formulaInputs.length === 0) {
          setError(
            "Calculated fields need a formula or at least one input field.",
          );
          return;
        }
        formula = {
          operation: formulaOperation,
          inputs: formulaInputs,
        };
      }
      const valueType =
        type === "Enum"
          ? `Enum[${distinctChoices.join(",")}]`
          : type === "Relationship"
            ? `Ref[${relationshipTarget}]`
            : type;
      if (type === "Relationship" && !relationshipTarget) {
        setError("Choose the table this relationship points to.");
        return;
      }
      await post("/api/questions", {
        name,
        subject_kind: kind,
        value_type: valueType,
        definition: {
          label: label || name,
          cardinality: many ? "many" : "one",
          volatility,
          freshness_days: freshness,
          ...(["Int", "Float", "Probability"].includes(valueType)
            ? {
                display_format: {
                  use_grouping: useGrouping,
                  precision,
                  digits: Number(formatDigits),
                },
              }
            : {}),
          ...(formula ? { formula } : {}),
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
              onChange={(event) => {
                const nextType = event.target.value;
                setType(nextType);
                if (nextType === "Relationship") setMany(true);
              }}
            >
              <option>String</option>
              <option>URL</option>
              <option>Int</option>
              <option>Float</option>
              <option>Probability</option>
              <option>Bool</option>
              <option>Year</option>
              <option>Date</option>
              <option value="Enum">Enum · fixed choices</option>
              <option value="Relationship">Relationship · another table</option>
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
        {["Int", "Float", "Probability"].includes(type) && (
          <fieldset className="display-format-fields">
            <legend>Display formatting</legend>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={useGrouping}
                onChange={(event) => setUseGrouping(event.target.checked)}
              />
              Use thousands separators
            </label>
            {type !== "Int" && (
              <div className="form-grid">
                <label>
                  Precision
                  <select
                    value={precision}
                    onChange={(event) => setPrecision(event.target.value)}
                  >
                    <option value="significant">Significant digits</option>
                    <option value="decimal">Decimal places</option>
                  </select>
                </label>
                <label>
                  Digits
                  <input
                    type="number"
                    min="1"
                    max="12"
                    value={formatDigits}
                    onChange={(event) => setFormatDigits(event.target.value)}
                    required
                  />
                </label>
              </div>
            )}
            <span className="format-preview">
              Preview:{" "}
              {formattedValue(type === "Int" ? 1234567 : 12345.6789, {
                question_id: "preview",
                name: "preview",
                value_type: type,
                definition: {
                  display_format: {
                    use_grouping: useGrouping,
                    precision,
                    digits: Number(formatDigits || 3),
                  },
                },
              })}
            </span>
          </fieldset>
        )}
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
        {type === "Relationship" && (
          <label>
            Related table
            <select
              value={relationshipTarget}
              onChange={(event) => setRelationshipTarget(event.target.value)}
              required
            >
              <option value="">Choose a table…</option>
              {entityKinds.map((entityKind) => (
                <option key={entityKind}>{entityKind}</option>
              ))}
            </select>
            <span className="field-hint">
              Each answer will reference a row in this table.
            </span>
          </label>
        )}
        <label className="checkbox">
          <input
            type="checkbox"
            checked={many}
            onChange={(event) => setMany(event.target.checked)}
          />
          {type === "Relationship"
            ? "Allow this row to link to multiple related rows"
            : "Allow multiple simultaneous answers"}
        </label>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={computed}
            onChange={(event) => {
              const checked = event.target.checked;
              setComputed(checked);
              if (checked && !["Int", "Float", "Probability"].includes(type))
                setType("Float");
            }}
          />
          Calculate this field from other fields
        </label>
        {computed && (
          <div className="formula-builder">
            <label className="formula-expression-field">
              Spreadsheet formula
              <input
                value={formulaExpression}
                onChange={(event) => setFormulaExpression(event.target.value)}
                placeholder="=C1/(2026-E1)"
              />
              <span className="field-hint">
                References are row-relative. Epiq stores stable field names, so
                the formula remains correct after columns move.
              </span>
            </label>
            <div className="formula-column-key">
              <span>
                <code>A</code> {kind} identity
              </span>
              {questions.map((question, index) => (
                <span key={question.question_id}>
                  <code>{spreadsheetColumn(index + 1)}</code>{" "}
                  {String(question.definition.label ?? question.name)}
                </span>
              ))}
            </div>
            <div className="formula-or">
              <span>or use the builder</span>
            </div>
            <label>
              Operation
              <select
                value={formulaOperation}
                onChange={(event) => setFormulaOperation(event.target.value)}
              >
                {["sum", "avg", "min", "max", "count", "divide"].map(
                  (operation) => (
                    <option key={operation}>{operation}</option>
                  ),
                )}
              </select>
            </label>
            <fieldset>
              <legend>Input fields</legend>
              {questions.length === 0 && (
                <span className="field-hint">Add source fields first.</span>
              )}
              {questions.map((question) => (
                <label className="checkbox" key={question.question_id}>
                  <input
                    type="checkbox"
                    checked={formulaInputs.includes(question.name)}
                    onChange={(event) =>
                      setFormulaInputs((current) =>
                        event.target.checked
                          ? [...current, question.name]
                          : current.filter((item) => item !== question.name),
                      )
                    }
                  />
                  {String(question.definition.label ?? question.name)}
                  <code>{question.value_type}</code>
                </label>
              ))}
            </fieldset>
          </div>
        )}
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

type RevisionPreview = {
  checked_values: number;
  compatible_values: number;
  incompatible_values: Array<{
    entity_id: string;
    entity_name: string;
    value: unknown;
    error: string;
  }>;
  can_apply: boolean;
};

function EditQuestionDialog({
  question,
  questions,
  onClose,
  onSaved,
}: {
  question: Question;
  questions: Question[];
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const originalFormula = question.definition.formula as
    { operation?: string; inputs?: string[]; expression?: string } | undefined;
  const isEnum = question.value_type.startsWith("Enum[");
  const [label, setLabel] = useState(
    String(question.definition.label ?? question.name),
  );
  const [type, setType] = useState(isEnum ? "Enum" : question.value_type);
  const [enumChoices, setEnumChoices] = useState(
    isEnum ? question.value_type.slice(5, -1) : "",
  );
  const [cardinality, setCardinality] = useState(
    String(question.definition.cardinality ?? "one"),
  );
  const [volatility, setVolatility] = useState(
    String(question.definition.volatility ?? "stable"),
  );
  const [freshnessDays, setFreshnessDays] = useState(
    question.definition.freshness_days == null
      ? ""
      : String(question.definition.freshness_days),
  );
  const [guidance, setGuidance] = useState(
    String(question.definition.research_guidance ?? ""),
  );
  const [computed, setComputed] = useState(Boolean(originalFormula));
  const [formulaOperation, setFormulaOperation] = useState(
    String(originalFormula?.operation ?? "sum"),
  );
  const [formulaInputs, setFormulaInputs] = useState<string[]>(
    Array.isArray(originalFormula?.inputs) ? originalFormula.inputs : [],
  );
  const inferredExpression =
    originalFormula?.operation === "divide" &&
    originalFormula.inputs?.length === 2
      ? `=${originalFormula.inputs
          .map((input) => {
            const index = questions.findIndex((item) => item.name === input);
            return index >= 0 ? `${spreadsheetColumn(index + 1)}1` : "";
          })
          .join("/")}`
      : "";
  const [formulaExpression, setFormulaExpression] = useState(
    String(originalFormula?.expression ?? inferredExpression),
  );
  const originalDisplayFormat = (question.definition.display_format ??
    {}) as Record<string, unknown>;
  const [useGrouping, setUseGrouping] = useState(
    originalDisplayFormat.use_grouping !== false,
  );
  const [precision, setPrecision] = useState(
    String(originalDisplayFormat.precision ?? "significant"),
  );
  const [formatDigits, setFormatDigits] = useState(
    String(originalDisplayFormat.digits ?? 3),
  );
  const [reason, setReason] = useState("");
  const [preview, setPreview] = useState<RevisionPreview | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const standardTypes = [
    "String",
    "URL",
    "Int",
    "Float",
    "Probability",
    "Bool",
    "Date",
    "DateTime",
    "Year",
    "Json",
    "Distribution[Float]",
  ];
  const valueType =
    type === "Enum"
      ? `Enum[${[
          ...new Set(
            enumChoices
              .split(",")
              .map((item) => item.trim())
              .filter(Boolean),
          ),
        ].join(",")}]`
      : type;
  let revisedFormula: Record<string, unknown> | undefined;
  let formulaError = "";
  if (computed && formulaExpression.trim()) {
    try {
      revisedFormula = parseSpreadsheetFormula(formulaExpression, questions);
    } catch (caught) {
      formulaError =
        caught instanceof Error ? caught.message : "Invalid formula";
    }
  } else if (computed) {
    revisedFormula = { operation: formulaOperation, inputs: formulaInputs };
    if (!formulaInputs.length)
      formulaError = "Choose at least one input field.";
  }
  const definition = {
    ...question.definition,
    label: label.trim() || question.name,
    cardinality,
    volatility,
    freshness_days: freshnessDays ? Number(freshnessDays) : null,
    research_guidance: guidance.trim(),
    formula: revisedFormula,
    display_format: ["Int", "Float", "Probability"].includes(valueType)
      ? {
          use_grouping: useGrouping,
          precision,
          digits: Number(formatDigits),
        }
      : undefined,
  };
  const body = { value_type: valueType, definition, reason };
  const review = async (event: FormEvent) => {
    event.preventDefault();
    if (formulaError) {
      setError(formulaError);
      return;
    }
    setBusy(true);
    setError("");
    try {
      setPreview(
        await post<RevisionPreview>(
          `/api/questions/${question.question_id}/revision-preview`,
          body,
        ),
      );
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not review changes",
      );
    } finally {
      setBusy(false);
    }
  };
  const apply = async () => {
    setBusy(true);
    setError("");
    try {
      await post(`/api/questions/${question.question_id}/revise`, body);
      onClose();
      await onSaved();
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not apply changes",
      );
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      title={`Edit ${String(question.definition.label ?? question.name)}`}
      subtitle="Changes create a new field version. Existing evidence and answers remain in history."
      onClose={onClose}
    >
      <form
        onSubmit={(event) => void review(event)}
        onChange={() => setPreview(null)}
      >
        <div className="schema-version-note">
          <span>Stable field key</span>
          <code>{question.name}</code>
        </div>
        <div className="form-grid">
          <label>
            Display label
            <input
              value={label}
              onChange={(event) => setLabel(event.target.value)}
              required
            />
          </label>
          <label>
            Value type
            <select
              value={type}
              onChange={(event) => setType(event.target.value)}
            >
              {!isEnum && !standardTypes.includes(question.value_type) && (
                <option>{question.value_type}</option>
              )}
              {standardTypes.map((item) => (
                <option key={item}>{item}</option>
              ))}
              <option value="Enum">Enum · fixed choices</option>
            </select>
          </label>
        </div>
        {type === "Enum" && (
          <label>
            Allowed choices
            <input
              value={enumChoices}
              onChange={(event) => setEnumChoices(event.target.value)}
              required
            />
          </label>
        )}
        {["Int", "Float", "Probability"].includes(type) && (
          <fieldset className="display-format-fields">
            <legend>Display formatting</legend>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={useGrouping}
                onChange={(event) => setUseGrouping(event.target.checked)}
              />
              Use thousands separators
            </label>
            {type !== "Int" && (
              <div className="form-grid">
                <label>
                  Precision
                  <select
                    value={precision}
                    onChange={(event) => setPrecision(event.target.value)}
                  >
                    <option value="significant">Significant digits</option>
                    <option value="decimal">Decimal places</option>
                  </select>
                </label>
                <label>
                  Digits
                  <input
                    type="number"
                    min="1"
                    max="12"
                    value={formatDigits}
                    onChange={(event) => setFormatDigits(event.target.value)}
                    required
                  />
                </label>
              </div>
            )}
            <span className="format-preview">
              Preview:{" "}
              {formattedValue(type === "Int" ? 1234567 : 12345.6789, {
                question_id: "preview",
                name: "preview",
                value_type: type,
                definition: {
                  display_format: {
                    use_grouping: useGrouping,
                    precision,
                    digits: Number(formatDigits || 3),
                  },
                },
              })}
            </span>
          </fieldset>
        )}
        <div className="form-grid">
          <label>
            Cardinality
            <select
              value={cardinality}
              onChange={(event) => setCardinality(event.target.value)}
            >
              <option value="one">One current answer</option>
              <option value="many">Multiple current answers</option>
            </select>
          </label>
          <label>
            Volatility
            <select
              value={volatility}
              onChange={(event) => setVolatility(event.target.value)}
            >
              <option value="stable">Stable</option>
              <option value="slow">Slow-changing</option>
              <option value="dynamic">Dynamic</option>
            </select>
          </label>
        </div>
        <label>
          Consider stale after <span>Days; blank means no expiry</span>
          <input
            type="number"
            min="1"
            value={freshnessDays}
            onChange={(event) => setFreshnessDays(event.target.value)}
          />
        </label>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={computed}
            onChange={(event) => setComputed(event.target.checked)}
          />
          Calculate this field from other fields
        </label>
        {computed && (
          <div className="formula-builder">
            <label className="formula-expression-field">
              Spreadsheet formula
              <input
                value={formulaExpression}
                onChange={(event) => setFormulaExpression(event.target.value)}
                placeholder="=C1/(2026-E1)"
              />
              <span className="field-hint">
                Editing creates a new field version; stable field-name
                references are stored beneath the spreadsheet notation.
              </span>
            </label>
            <div className="formula-column-key">
              <span>
                <code>A</code> Row identity
              </span>
              {questions.map((candidate, index) => (
                <span key={candidate.question_id}>
                  <code>{spreadsheetColumn(index + 1)}</code>{" "}
                  {String(candidate.definition.label ?? candidate.name)}
                </span>
              ))}
            </div>
            <div className="formula-or">
              <span>or use the builder</span>
            </div>
            <label>
              Operation
              <select
                value={formulaOperation}
                onChange={(event) => setFormulaOperation(event.target.value)}
              >
                {["sum", "avg", "min", "max", "count", "divide"].map(
                  (operation) => (
                    <option key={operation}>{operation}</option>
                  ),
                )}
              </select>
            </label>
            <fieldset>
              <legend>Input fields</legend>
              {questions
                .filter((candidate) => candidate.name !== question.name)
                .map((candidate) => (
                  <label className="checkbox" key={candidate.question_id}>
                    <input
                      type="checkbox"
                      checked={formulaInputs.includes(candidate.name)}
                      onChange={(event) =>
                        setFormulaInputs((current) =>
                          event.target.checked
                            ? [...current, candidate.name]
                            : current.filter((item) => item !== candidate.name),
                        )
                      }
                    />
                    {String(candidate.definition.label ?? candidate.name)}
                    <code>{candidate.value_type}</code>
                  </label>
                ))}
            </fieldset>
          </div>
        )}
        <label>
          Instructions for researchers <span>Optional</span>
          <textarea
            value={guidance}
            onChange={(event) => setGuidance(event.target.value)}
          />
        </label>
        <label>
          Why are you changing the schema?
          <textarea
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            required
          />
        </label>
        {preview && (
          <div
            className={`revision-preview ${preview.can_apply ? "compatible" : "incompatible"}`}
          >
            <b>
              {preview.can_apply
                ? "✓ Ready to apply"
                : "⚠ Existing values need attention"}
            </b>
            <p>
              Checked {preview.checked_values} current value
              {preview.checked_values === 1 ? "" : "s"};{" "}
              {preview.compatible_values} match {valueType}.
            </p>
            {preview.incompatible_values.map((item) => (
              <div key={`${item.entity_id}:${JSON.stringify(item.value)}`}>
                <strong>{item.entity_name}</strong>
                <code>{display(item.value)}</code>
                <small>{item.error}</small>
              </div>
            ))}
          </div>
        )}
        {error && <div className="form-error">{error}</div>}
        <div className="modal-actions">
          <button type="button" className="ghost" onClick={onClose}>
            Cancel
          </button>
          {!preview ? (
            <button className="primary" disabled={busy || !reason.trim()}>
              {busy ? "Checking…" : "Review changes →"}
            </button>
          ) : (
            <button
              type="button"
              className="primary"
              disabled={busy || !preview.can_apply}
              onClick={() => void apply()}
            >
              {busy ? "Applying…" : "Apply new version"}
            </button>
          )}
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
  const relationshipTarget =
    selection.question.value_type.match(/^Ref\[(.+)\]$/)?.[1];
  const [relatedRows, setRelatedRows] = useState<Matrix["rows"]>([]);
  const [sourceType, setSourceType] = useState("web");
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [excerpt, setExcerpt] = useState("");
  const [confidence, setConfidence] = useState("high");
  const [error, setError] = useState("");
  const enumOptions = selection.question.value_type.startsWith("Enum[")
    ? selection.question.value_type.slice(5, -1).split(",")
    : [];
  useEffect(() => {
    if (!relationshipTarget) return;
    void api<Matrix>(`/api/matrix/${encodeURIComponent(relationshipTarget)}`)
      .then((related) => setRelatedRows(related.rows))
      .catch((caught) =>
        setError(
          caught instanceof Error
            ? caught.message
            : "Could not load related rows",
        ),
      );
  }, [relationshipTarget]);
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
          {relationshipTarget ? (
            <>
              <select
                value={value}
                onChange={(event) => setValue(event.target.value)}
                autoFocus
                required
              >
                <option value="">Choose {relationshipTarget}…</option>
                {relatedRows.map((row) => (
                  <option key={row.entity_id} value={row.entity_id}>
                    {row.name}
                  </option>
                ))}
              </select>
              {relatedRows.length === 0 && (
                <span className="field-hint">
                  Add rows to the {relationshipTarget} table before creating
                  this relationship.
                </span>
              )}
              {selection.question.definition.cardinality === "many" && (
                <span className="field-hint">
                  Save one related row at a time; reopen the cell to add
                  another.
                </span>
              )}
            </>
          ) : selection.question.value_type === "Bool" ? (
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
              type={
                selection.question.value_type === "URL"
                  ? "url"
                  : selection.question.value_type === "Date"
                    ? "date"
                    : selection.question.value_type === "Year"
                      ? "number"
                      : "text"
              }
              min={selection.question.value_type === "Year" ? 1 : undefined}
              max={selection.question.value_type === "Year" ? 9999 : undefined}
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

function FillSelectionDialog({
  rows,
  question,
  onClose,
  onContinue,
}: {
  rows: Matrix["rows"];
  question: Question;
  onClose: () => void;
  onContinue: (claims: PastedClaim[]) => void;
}) {
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const enumOptions = question.value_type.startsWith("Enum[")
    ? question.value_type.slice(5, -1).split(",")
    : [];
  const submit = (event: FormEvent) => {
    event.preventDefault();
    try {
      const parsed = parseValue(value, question.value_type);
      onContinue(
        rows.map((row) => ({
          entityId: row.entity_id,
          entityName: row.name,
          question,
          rawValue: value,
          value: parsed,
          existingState: row.cells[question.name].state,
        })),
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Invalid value");
    }
  };
  return (
    <Modal
      title={`Fill ${rows.length} selected cells`}
      subtitle={`${String(question.definition.label ?? question.name)} · ${question.value_type}. Evidence is attached in the next step.`}
      onClose={onClose}
    >
      <form onSubmit={submit}>
        <label>
          Value for every selected row
          {question.value_type === "Bool" ? (
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
          ) : enumOptions.length ? (
            <select
              value={value}
              onChange={(event) => setValue(event.target.value)}
              autoFocus
              required
            >
              <option value="">Choose…</option>
              {enumOptions.map((option) => (
                <option key={option}>{option}</option>
              ))}
            </select>
          ) : (
            <input
              type={
                question.value_type === "Date"
                  ? "date"
                  : question.value_type === "Year"
                    ? "number"
                    : "text"
              }
              min={question.value_type === "Year" ? 1 : undefined}
              max={question.value_type === "Year" ? 9999 : undefined}
              value={value}
              onChange={(event) => setValue(event.target.value)}
              autoFocus
              required
            />
          )}
        </label>
        <div className="fill-target-list">
          {rows.slice(0, 6).map((row) => (
            <span key={row.entity_id}>{row.name}</span>
          ))}
          {rows.length > 6 && <span>+{rows.length - 6} more</span>}
        </div>
        {error && <div className="form-error">{error}</div>}
        <div className="modal-actions">
          <button type="button" className="ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="primary">Continue to evidence →</button>
        </div>
      </form>
    </Modal>
  );
}

function PasteDialog({
  claims,
  onClose,
  onSaved,
}: {
  claims: PastedClaim[];
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const [sourceType, setSourceType] = useState("report");
  const [title, setTitle] = useState("Spreadsheet batch entry");
  const [url, setUrl] = useState("");
  const [excerpt, setExcerpt] = useState("");
  const [confidence, setConfidence] = useState("high");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const invalid = claims.filter((claim) => claim.error);
  const existing = claims.filter(
    (claim) =>
      claim.existingState !== "Unasked" && claim.existingState !== "NotFound",
  );
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (invalid.length) return;
    setBusy(true);
    setError("");
    try {
      const evidenceRef = "pasted_source";
      await post("/api/batch", {
        actor: "human:web-paste",
        operations: [
          {
            op: "evidence.add",
            ref: evidenceRef,
            source_type: sourceType,
            url: sourceType === "web" ? url : `urn:epiq:${sourceType}`,
            title,
            retrieved_at: today(),
            excerpt,
          },
          ...claims.map((claim) => ({
            op: "claim.assert",
            subject: claim.entityId,
            question: claim.question.question_id,
            value: claim.value,
            valid_from: today(),
            evidence_refs: [evidenceRef],
            confidence,
            temporal_basis:
              sourceType === "web" || sourceType === "report"
                ? "source"
                : "observed",
          })),
        ],
      });
      await onSaved();
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not paste values",
      );
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      title={`Add ${claims.length} sourced value${claims.length === 1 ? "" : "s"}`}
      subtitle="Review type conversion and attach shared evidence. The entire paste commits atomically."
      onClose={onClose}
    >
      <form onSubmit={(event) => void submit(event)}>
        <div className="paste-preview">
          <div className="paste-preview-heading">
            <b>Preview</b>
            <span>
              {invalid.length
                ? `${invalid.length} invalid`
                : "All values valid"}
            </span>
          </div>
          {claims.map((claim, index) => (
            <div
              className={claim.error ? "paste-row invalid" : "paste-row"}
              key={`${claim.entityId}:${claim.question.question_id}:${index}`}
            >
              <span>{claim.entityName}</span>
              <span>
                {String(claim.question.definition.label ?? claim.question.name)}
              </span>
              <code>{claim.rawValue}</code>
              <small>{claim.error ?? claim.question.value_type}</small>
            </div>
          ))}
        </div>
        {existing.length > 0 && (
          <div className="form-warning">
            {existing.length} value{existing.length === 1 ? "" : "s"} will be
            added to cells that already have research. Epiq preserves the
            existing claims and may mark conflicting answers as contested.
          </div>
        )}
        <div className="form-grid">
          <label>
            Evidence kind
            <select
              value={sourceType}
              onChange={(event) => setSourceType(event.target.value)}
            >
              <option value="report">Report or document</option>
              <option value="web">Web page</option>
              <option value="personal">Personal knowledge</option>
              <option value="model">Model output</option>
              <option value="interview">Interview or conversation</option>
              <option value="other">Other evidence</option>
            </select>
          </label>
          <label>
            Source title
            <input
              value={title}
              onChange={(event) => setTitle(event.target.value)}
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
              required
            />
          </label>
        )}
        <label>
          Evidence excerpt or provenance note
          <textarea
            value={excerpt}
            onChange={(event) => setExcerpt(event.target.value)}
            placeholder="Describe where this pasted table came from and what the values represent."
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
          <button className="primary" disabled={busy || invalid.length > 0}>
            {busy ? "Writing batch…" : `Add ${claims.length} sourced values`}
          </button>
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
          Add to Control Center →
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
          Add to Control Center →
        </button>
      </div>
    </Modal>
  );
}

function ReviewPanel({
  stale,
  contradictions,
  staleDerivations,
  provisionalRelationships,
  onClose,
  onInspect,
  onRecalculate,
  onInspectProvisional,
  onAcceptProvisional,
  onAcceptAllProvisional,
  onRejectProvisional,
  onRejectAllProvisional,
}: {
  stale: DiagnosticCell[];
  contradictions: DiagnosticCell[];
  staleDerivations: StaleDerivation[];
  provisionalRelationships: ProvisionalRelationship[];
  onClose: () => void;
  onInspect: (item: DiagnosticCell) => void;
  onRecalculate: (item: StaleDerivation) => Promise<void>;
  onInspectProvisional: (item: ProvisionalRelationship) => void;
  onAcceptProvisional: (items: ProvisionalRelationship[]) => Promise<void>;
  onAcceptAllProvisional: () => Promise<void>;
  onRejectProvisional: (items: ProvisionalRelationship[]) => Promise<void>;
  onRejectAllProvisional: () => Promise<void>;
}) {
  const [tab, setTab] = useState<
    "provisional" | "contradictions" | "stale" | "calculations"
  >(
    provisionalRelationships.length
      ? "provisional"
      : contradictions.length
        ? "contradictions"
        : stale.length
          ? "stale"
          : "calculations",
  );
  const [busy, setBusy] = useState("");
  const provisionalCells = useMemo(() => {
    const groups = new Map<string, ProvisionalRelationship[]>();
    provisionalRelationships.forEach((suggestion) => {
      const key = `${suggestion.subject_entity_id}:${suggestion.question_id}`;
      groups.set(key, [...(groups.get(key) ?? []), suggestion]);
    });
    return [...groups.values()];
  }, [provisionalRelationships]);
  const tabs = [
    ["provisional", "Provisional", provisionalRelationships.length],
    ["contradictions", "Contradictions", contradictions.length],
    ["stale", "Stale evidence", stale.length],
    ["calculations", "Calculations", staleDerivations.length],
  ] as const;
  const items = tab === "contradictions" ? contradictions : stale;
  return (
    <aside className="activity-panel review-panel">
      <div className="drawer-head">
        <div className="eyebrow">REVIEW QUEUE</div>
        <button className="close" onClick={onClose}>
          ×
        </button>
        <h2>Needs attention</h2>
        <p>
          Review agent proposals, disagreements, stale evidence, and derived
          values.
        </p>
      </div>
      <div className="review-tabs">
        {tabs.map(([key, label, count]) => (
          <button
            key={key}
            className={tab === key ? "active" : ""}
            onClick={() => setTab(key)}
          >
            {label}
            <span>{count}</span>
          </button>
        ))}
      </div>
      <div className="activity-list review-list">
        {tab === "provisional" && provisionalRelationships.length > 0 && (
          <div className="review-bulk-bar">
            <span>
              {provisionalCells.length} cell
              {provisionalCells.length === 1 ? "" : "s"}
            </span>
            <div>
              <button
                disabled={Boolean(busy)}
                onClick={() => void onRejectAllProvisional()}
              >
                Reject all
              </button>
              <button
                className="primary"
                disabled={Boolean(busy)}
                onClick={async () => {
                  setBusy("all-provisional");
                  try {
                    await onAcceptAllProvisional();
                  } finally {
                    setBusy("");
                  }
                }}
              >
                {busy === "all-provisional"
                  ? "Accepting…"
                  : `Accept all ${provisionalRelationships.length}`}
              </button>
            </div>
          </div>
        )}
        {tab === "provisional" &&
          provisionalCells.map((items) => {
            const first = items[0];
            const key = `${first.subject_entity_id}:${first.question_id}`;
            return (
              <article
                className="review-card provisional-review-card"
                key={key}
              >
                <div className="review-card-head">
                  <span className="review-kind provisional">PROVISIONAL</span>
                  <small>
                    {items.length} entr{items.length === 1 ? "y" : "ies"}
                  </small>
                </div>
                <h3>{first.subject_name}</h3>
                <p>{first.question_name.replaceAll("_", " ")}</p>
                <div className="provisional-review-values">
                  {items.map((item) => (
                    <code key={item.suggestion_id}>{item.target_name}</code>
                  ))}
                </div>
                <div className="review-card-actions">
                  <button onClick={() => onInspectProvisional(first)}>
                    Inspect
                  </button>
                  <button
                    disabled={Boolean(busy)}
                    onClick={() => void onRejectProvisional(items)}
                  >
                    Reject all
                  </button>
                  <button
                    className="primary"
                    disabled={busy === key}
                    onClick={async () => {
                      setBusy(key);
                      try {
                        await onAcceptProvisional(items);
                      } finally {
                        setBusy("");
                      }
                    }}
                  >
                    {busy === key ? "Accepting…" : `Accept all ${items.length}`}
                  </button>
                </div>
              </article>
            );
          })}
        {tab !== "calculations" &&
          tab !== "provisional" &&
          items.map((item) => (
            <article
              className="review-card"
              key={`${tab}:${item.entity_id}:${item.question_id}`}
            >
              <div className="review-card-head">
                <span className={`review-kind ${tab}`}>
                  {tab === "stale" ? "STALE" : "CONTESTED"}
                </span>
                {item.temporal?.as_of && (
                  <small>as of {item.temporal.as_of}</small>
                )}
              </div>
              <h3>{item.entity_name}</h3>
              <p>{item.question.replaceAll("_", " ")}</p>
              {item.values.length > 0 && (
                <div className="review-values">
                  {item.values.map((value, index) => (
                    <code key={index}>{display(value)}</code>
                  ))}
                </div>
              )}
              <button className="primary" onClick={() => onInspect(item)}>
                Inspect and resolve
              </button>
            </article>
          ))}
        {tab === "calculations" &&
          staleDerivations.map((item) => (
            <article className="review-card" key={item.claim_id}>
              <div className="review-card-head">
                <span className="review-kind calculations">STALE ƒ</span>
                <code>{item.claim_id}</code>
              </div>
              <h3>{item.subject}</h3>
              <p>{item.question.replaceAll("_", " ")}</p>
              <small>
                {item.reasons.length} changed dependenc
                {item.reasons.length === 1 ? "y" : "ies"}
              </small>
              <button
                className="primary"
                disabled={busy === item.claim_id}
                onClick={async () => {
                  setBusy(item.claim_id);
                  try {
                    await onRecalculate(item);
                  } finally {
                    setBusy("");
                  }
                }}
              >
                {busy === item.claim_id ? "Calculating…" : "Recalculate field"}
              </button>
            </article>
          ))}
        {((tab === "provisional" && provisionalCells.length === 0) ||
          (tab !== "calculations" &&
            tab !== "provisional" &&
            items.length === 0) ||
          (tab === "calculations" && staleDerivations.length === 0)) && (
          <div className="drawer-empty review-empty">
            <div>✓</div>
            <p>Nothing in this queue.</p>
          </div>
        )}
      </div>
    </aside>
  );
}

function WorkspaceAgentPanel({
  jobs,
  curator,
  draft,
  onDraftConsumed,
  onClose,
  onSend,
  onCancel,
  onApprove,
  onReject,
  onStartCurator,
  onControlCurator,
}: {
  jobs: ResearchJob[];
  curator: CuratorState | null;
  draft: string;
  onDraftConsumed: () => void;
  onClose: () => void;
  onSend: (message: string, autoApprove: boolean) => Promise<void>;
  onCancel: (jobId: string) => Promise<void>;
  onApprove: (jobId: string) => Promise<void>;
  onReject: (jobId: string) => Promise<void>;
  onStartCurator: (
    objective: string,
    maxCycles: number,
    maxResearchCells: number,
  ) => Promise<void>;
  onControlCurator: (action: "pause" | "resume" | "stop") => Promise<void>;
}) {
  const [message, setMessage] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState("");
  const [curatorObjective, setCuratorObjective] = useState("");
  const [curatorStarting, setCuratorStarting] = useState(false);
  const [yoloMode, setYoloMode] = useState(
    () => window.localStorage.getItem("epiq-control-center-yolo") === "true",
  );
  const listRef = useRef<HTMLDivElement>(null);
  const followConversationTail = useRef(true);
  useEffect(() => {
    if (!draft) return;
    setMessage(draft);
    onDraftConsumed();
  }, [draft, onDraftConsumed]);
  const ordered = jobs
    .map((job, index) => ({ job, index }))
    .filter(({ job }) => job.job_type === "workspace_agent")
    .sort((left, right) => {
      const leftLive = left.job.created_at ? 1 : 0;
      const rightLive = right.job.created_at ? 1 : 0;
      if (leftLive !== rightLive) return leftLive - rightLive;
      if (left.job.created_at && right.job.created_at) {
        return left.job.created_at.localeCompare(right.job.created_at);
      }
      return left.index - right.index;
    })
    .map(({ job }) => job);
  const jobsById = new Map(jobs.map((job) => [job.job_id, job]));
  useEffect(() => {
    const list = listRef.current;
    if (list && followConversationTail.current) {
      const frame = window.requestAnimationFrame(() => {
        list.scrollTop = list.scrollHeight;
      });
      return () => window.cancelAnimationFrame(frame);
    }
  }, [jobs]);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const value = message.trim();
    if (!value || sending) return;
    setSending(true);
    setSendError("");
    followConversationTail.current = true;
    try {
      await onSend(value, yoloMode);
      setMessage("");
    } catch (error) {
      setSendError(error instanceof Error ? error.message : "Request failed");
    } finally {
      setSending(false);
    }
  };
  return (
    <aside className="workspace-agent-panel">
      <div className="drawer-head">
        <button className="close" onClick={onClose}>×</button>
        <h2>Control Center</h2>
      </div>
      <div className="curator-control">
        {curator && ["running", "paused", "stopping"].includes(curator.status) ? (
          <div className="curator-status">
            <div>
              <b>Curator · {curator.status}</b>
              <small>
                {curator.completed_cycles}/{curator.max_cycles} cycles · {curator.research_cells_used}/{curator.max_research_cells} research cells
              </small>
            </div>
            <p>{curator.message}</p>
            <div>
              {curator.status === "running" && (
                <button onClick={() => void onControlCurator("pause")}>Pause</button>
              )}
              {curator.status === "paused" && (
                <button onClick={() => void onControlCurator("resume")}>Resume</button>
              )}
              {curator.status !== "stopping" && (
                <button onClick={() => void onControlCurator("stop")}>Stop</button>
              )}
            </div>
          </div>
        ) : (
          <details>
            <summary>Keep enriching this project</summary>
            {curator && <small>{curator.message}</small>}
            <textarea
              value={curatorObjective}
              onChange={(event) => setCuratorObjective(event.target.value)}
              placeholder="What should this dataset become useful for?"
              rows={2}
            />
            <div className="curator-budget">
              <span>Up to 5 cycles · 50 research cells</span>
              <button
                disabled={curatorStarting || !curatorObjective.trim()}
                onClick={async () => {
                  setCuratorStarting(true);
                  try {
                    await onStartCurator(curatorObjective.trim(), 5, 50);
                    setCuratorObjective("");
                  } finally {
                    setCuratorStarting(false);
                  }
                }}
              >
                {curatorStarting ? "Starting…" : "Start curator"}
              </button>
            </div>
          </details>
        )}
      </div>
      <div
        className="agent-conversation"
        ref={listRef}
        onScroll={(event) => {
          const list = event.currentTarget;
          followConversationTail.current =
            list.scrollHeight - list.scrollTop - list.clientHeight < 48;
        }}
      >
        {ordered.length === 0 && (
          <p className="agent-empty">Tell Epiq what to research or change.</p>
        )}
        {ordered.map((job) => {
          const children = (job.child_job_ids ?? [])
            .map((id) => jobsById.get(id))
            .filter((item): item is ResearchJob => Boolean(item));
          const finishedChildren = children.filter((item) =>
            ["completed", "failed", "cancelled"].includes(item.status),
          );
          const failedChildren = children.filter((item) => item.status === "failed");
          const activeChildren = children.filter((item) =>
            ["queued", "running"].includes(item.status),
          );
          const childWrites = children.reduce((total, item) => total + (item.written ?? 0), 0);
          const isWorking = ["queued", "running"].includes(job.status) || activeChildren.length > 0;
          return <div className="agent-exchange" key={job.job_id}>
            <div className="agent-message user-message">{job.user_message ?? job.instructions}</div>
            <div className={`agent-message assistant-message ${isWorking ? "running" : job.status}`}>
              <div className="agent-message-status">
                <span className={`job-status ${isWorking ? "running" : job.status}`}>
                  {isWorking ? "working" : job.status}
                </span>
                {isWorking && <i className="agent-spinner" />}
              </div>
              {job.assistant_summary ? <p>{job.assistant_summary}</p> : (
                <p>{job.messages.at(-1)?.message ?? "Preparing the workspace…"}</p>
              )}
              {isWorking && job.assistant_summary && job.messages.at(-1)?.message && (
                <small className="agent-live-status">
                  {job.messages.at(-1)?.message}
                </small>
              )}
              {job.workspace_plan && (
                <>
                  <div className="agent-plan-summary">
                    <span>{job.workspace_plan.entity_kinds.length} tables</span>
                    <span>{job.workspace_plan.entities.length} rows</span>
                    <span>{job.workspace_plan.questions.length} fields</span>
                    {(job.workspace_plan.retractions?.length ?? 0) > 0 && (
                      <span>{job.workspace_plan.retractions?.length} retractions</span>
                    )}
                    {(job.workspace_plan.replacements?.length ?? 0) > 0 && (
                      <span>{job.workspace_plan.replacements?.length} replacements</span>
                    )}
                    <span>{job.estimated_research_cells ?? job.child_job_ids?.length ?? 0} research cells</span>
                  </div>
                  {job.approval_status === "pending" && (
                    <div className="workspace-plan-preview">
                      {job.workspace_plan.entity_kinds.map((entityKind) => (
                        <div key={entityKind}>
                          <b>{entityKind}</b>
                          <small>
                            {job.workspace_plan?.entities
                              .filter((item) => item.kind === entityKind)
                              .map((item) => item.name)
                              .join(", ") || "No initial rows"}
                          </small>
                          <span>
                            {job.workspace_plan?.questions
                              .filter((item) => item.kind === entityKind)
                              .map((item) => item.label || item.name)
                              .join(" · ") || "No fields"}
                          </span>
                        </div>
                      ))}
                      <p>Nothing has been added yet. Approval creates this schema and starts the proposed research.</p>
                    </div>
                  )}
                  {job.approval_status === "rejected" && (
                    <small className="plan-dismissed">Plan dismissed without changing the workspace.</small>
                  )}
                </>
              )}
              {job.command_transcript && job.command_transcript.length > 0 && (
                <details className="agent-command-transcript">
                  <summary>{job.command_transcript.length} proposed CLI command{job.command_transcript.length === 1 ? "" : "s"}</summary>
                  {job.command_transcript.map((command, index) => (
                    <pre key={`${job.job_id}:command:${index}`}><code>$ {command.argv.map((part) => JSON.stringify(part)).join(" ")}</code></pre>
                  ))}
                </details>
              )}
              {job.approval_status === "pending" && (
                <div className="workspace-plan-actions">
                  <button className="primary" onClick={() => void onApprove(job.job_id)}>Approve plan</button>
                  <button onClick={() => void onReject(job.job_id)}>Dismiss</button>
                </div>
              )}
              {job.command_results && job.command_results.length > 0 && (
                <details className="agent-command-transcript">
                  <summary>{job.command_results.length} command result{job.command_results.length === 1 ? "" : "s"}</summary>
                  {job.command_results.map((command, index) => (
                    <div key={`${job.job_id}:result:${index}`}>
                      <pre><code>$ {command.argv.map((part) => JSON.stringify(part)).join(" ")}</code></pre>
                      <pre><code>{JSON.stringify(command.result, null, 2)}</code></pre>
                    </div>
                  ))}
                </details>
              )}
              {children.length > 0 && (
                <div className="agent-child-progress">
                  <div>
                    <b>{finishedChildren.length} of {children.length} cells researched</b>
                    <span>{childWrites} updated{failedChildren.length ? ` · ${failedChildren.length} failed` : ""}</span>
                  </div>
                  <div className="job-progress">
                    <i style={{ width: `${(finishedChildren.length / children.length) * 100}%` }} />
                  </div>
                  {activeChildren.length > 0 && (
                    <small>{activeChildren.slice(0, 3).map((item) => item.messages.at(-1)?.message).filter(Boolean).join(" · ")}</small>
                  )}
                </div>
              )}
              {job.error && <div className="form-error">{job.error}</div>}
              {isWorking && (
                <button className="job-action" onClick={() => {
                  const targets = activeChildren.length ? activeChildren : [job];
                  void Promise.all(targets.map((item) => onCancel(item.job_id)));
                }}>Cancel active work</button>
              )}
            </div>
          </div>;
        })}
      </div>
      <form className="agent-composer" onSubmit={submit}>
        <textarea
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          placeholder="What should Epiq do?"
          rows={3}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              event.currentTarget.form?.requestSubmit();
            }
          }}
        />
        <div>
          <label
            className={`yolo-toggle ${yoloMode ? "enabled" : ""}`}
            title="Execute proposed CLI commands immediately without a separate approval step"
          >
            <input
              type="checkbox"
              checked={yoloMode}
              onChange={(event) => {
                const enabled = event.target.checked;
                setYoloMode(enabled);
                window.localStorage.setItem(
                  "epiq-control-center-yolo",
                  String(enabled),
                );
              }}
            />
            <span>YOLO mode</span>
            <small>{yoloMode ? "Build immediately" : "Ask before building"}</small>
          </label>
          <button disabled={sending || !message.trim()}>{sending ? "Starting…" : "Send direction ✦"}</button>
        </div>
        <small className="composer-key-hint">Enter to send · Shift+Enter for a new line</small>
        {sendError && <div className="form-error">{sendError}</div>}
      </form>
    </aside>
  );
}

function ActivityPanel({
  jobs,
  onClose,
  onSuggestion,
  onAcceptFields,
  onAcceptRelationships,
  onAcceptSchemaAdaptation,
  onCancel,
  onRetry,
}: {
  jobs: ResearchJob[];
  onClose: () => void;
  onSuggestion: (
    jobId: string,
    suggestionId: string,
    action: "accept" | "dismiss",
  ) => Promise<void>;
  onAcceptFields: (jobId: string, suggestionIds: string[]) => Promise<void>;
  onAcceptRelationships: (
    jobId: string,
    suggestionIds: string[],
  ) => Promise<void>;
  onAcceptSchemaAdaptation: (questionId: string) => Promise<void>;
  onCancel: (jobId: string) => Promise<void>;
  onRetry: (jobId: string) => Promise<void>;
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
        <SchemaAdaptationReviews
          jobs={jobs}
          onAccept={onAcceptSchemaAdaptation}
        />
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
                {job.mode === "workspace_agent"
                  ? "Workspace"
                  : job.mode === "suggest_entities" ||
                job.mode === "suggest_fields"
                  ? job.entity_kind
                  : job.question_id.replace(/^q_|_v\d+$/g, "")}
              </code>
            </div>
            <b>
              {job.mode === "suggest_entities"
                ? `Find ${job.entity_kind.toLowerCase()} rows`
                : job.mode === "suggest_fields"
                  ? `Suggest fields for ${job.entity_kind}`
                  : job.mode === "workspace_agent"
                    ? "Build workspace"
                  : job.mode === "retry_not_found"
                    ? "Correct and retry NotFound"
                    : job.mode === "add_evidence"
                      ? "Get more evidence"
                      : "Fill missing values"}
            </b>
            {job.mode === "suggest_entities" && job.instructions && (
              <blockquote className="entity-query-summary">
                “{job.instructions}”
              </blockquote>
            )}
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
            {job.status === "completed" && job.outcome === "no_change" && (
              <div className="job-result no-change">
                <b>No new independent evidence was added.</b>
                <p>
                  {job.messages.at(-1)?.message ??
                    `${job.no_result ?? 0} search returned no result.`}
                </p>
                {job.rejected ? (
                  <small>{job.rejected} duplicate source was rejected.</small>
                ) : null}
              </div>
            )}
            {job.status === "completed" && job.outcome === "changed" && (
              <div className="job-result changed">
                Added evidence or answers to {job.written ?? 0} row
                {job.written === 1 ? "" : "s"}.
              </div>
            )}
            {(job.status === "queued" || job.status === "running") && (
              <button
                className="job-action"
                onClick={() => void onCancel(job.job_id)}
              >
                Cancel
              </button>
            )}
            {(job.status === "failed" || job.status === "cancelled") && (
              <button
                className="job-action"
                onClick={() => void onRetry(job.job_id)}
              >
                Retry
              </button>
            )}
            {job.suggestions && job.suggestions.length > 0 && (
              <EntitySuggestionReview job={job} onUpdate={onSuggestion} />
            )}
            {job.field_suggestions && job.field_suggestions.length > 0 && (
              <FieldSuggestionReview job={job} onAccept={onAcceptFields} />
            )}
            {!job.schema_adaptation &&
              job.relationship_suggestions &&
              job.relationship_suggestions.length > 0 && (
                <RelationshipSuggestionReview
                  job={job}
                  onAccept={onAcceptRelationships}
                />
              )}
          </article>
        ))}
      </div>
    </aside>
  );
}

function RelationshipSuggestionReview({
  job,
  onAccept,
}: {
  job: ResearchJob;
  onAccept: (jobId: string, suggestionIds: string[]) => Promise<void>;
}) {
  const pending =
    job.relationship_suggestions?.filter((item) => item.status === "pending") ??
    [];
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setSelected((current) => {
      const valid = new Set(pending.map((item) => item.suggestion_id));
      const retained = current.filter((item) => valid.has(item));
      if (retained.length || current.length) return retained;
      return [...valid];
    });
  }, [job.relationship_suggestions]);
  const toggle = (id: string) =>
    setSelected((current) =>
      current.includes(id)
        ? current.filter((item) => item !== id)
        : [...current, id],
    );
  return (
    <div className="relationship-suggestion-review">
      <div className="proposal-heading">
        <b>Proposed relationships</b>
        <small>Nothing is added until you approve it.</small>
      </div>
      {job.relationship_suggestions?.map((suggestion) => (
        <label
          className={`relationship-suggestion-card ${suggestion.status}`}
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
            <b>{suggestion.subject_name}</b>
            <span className="relationship-arrow">→</span>
            <strong>{suggestion.target_name}</strong>
            <em>
              {suggestion.action === "link"
                ? `link existing ${suggestion.target_kind}`
                : `create ${suggestion.target_kind} + link`}
            </em>
            {suggestion.proposed_fields &&
              Object.keys(suggestion.proposed_fields).length > 0 && (
                <dl className="relationship-field-preview">
                  {Object.entries(suggestion.proposed_fields).map(([name, value]) => (
                    <div key={name}>
                      <dt>{name.replaceAll("_", " ")}</dt>
                      <dd>{Array.isArray(value) ? value.join(", ") : String(value)}</dd>
                    </div>
                  ))}
                </dl>
              )}
            <blockquote>{suggestion.excerpt}</blockquote>
            {suggestion.source_url ? (
              <a href={suggestion.source_url} target="_blank" rel="noreferrer">
                ↗ {suggestion.source_title}
              </a>
            ) : (
              <small>{suggestion.source_title}</small>
            )}
          </span>
        </label>
      ))}
      {pending.length > 0 && (
        <button
          className="primary add-relationship-proposals"
          disabled={!selected.length || busy}
          onClick={async () => {
            setBusy(true);
            try {
              await onAccept(job.job_id, selected);
            } finally {
              setBusy(false);
            }
          }}
        >
          {busy
            ? "Adding relationships…"
            : `Add ${selected.length} selected relationship${selected.length === 1 ? "" : "s"}`}
        </button>
      )}
    </div>
  );
}

function EntitySuggestionReview({
  job,
  onUpdate,
}: {
  job: ResearchJob;
  onUpdate: (
    jobId: string,
    suggestionId: string,
    action: "accept" | "dismiss",
  ) => Promise<void>;
}) {
  const pending =
    job.suggestions?.filter((item) => item.status === "pending") ?? [];
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setSelected((current) => {
      const valid = new Set(pending.map((item) => item.suggestion_id));
      const retained = current.filter((item) => valid.has(item));
      if (retained.length || current.length) return retained;
      return [...valid];
    });
  }, [job.suggestions]);

  const toggle = (suggestionId: string) =>
    setSelected((current) =>
      current.includes(suggestionId)
        ? current.filter((item) => item !== suggestionId)
        : [...current, suggestionId],
    );

  const acceptSelected = async () => {
    setBusy(true);
    try {
      for (const suggestionId of selected) {
        await onUpdate(job.job_id, suggestionId, "accept");
      }
      setSelected([]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="entity-suggestion-review">
      {job.suggestions?.map((suggestion) => (
        <label
          className={`suggestion-card ${suggestion.status}`}
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
            <span className="suggestion-title">
              <b>{suggestion.name}</b>
              <em className={`suggestion-state ${suggestion.status}`}>
                {suggestion.status}
              </em>
            </span>
            <p>{suggestion.rationale}</p>
            <a href={suggestion.source_url} target="_blank" rel="noreferrer">
              ↗ {suggestion.source_title}
            </a>
            {suggestion.status === "pending" && (
              <button
                className="dismiss-suggestion"
                onClick={(event) => {
                  event.preventDefault();
                  void onUpdate(
                    job.job_id,
                    suggestion.suggestion_id,
                    "dismiss",
                  );
                }}
              >
                Dismiss
              </button>
            )}
          </span>
        </label>
      ))}
      {pending.length > 0 && (
        <button
          className="primary add-selected-entities"
          disabled={!selected.length || busy}
          onClick={() => void acceptSelected()}
        >
          {busy
            ? "Adding rows…"
            : `Add ${selected.length} selected row${selected.length === 1 ? "" : "s"}`}
        </button>
      )}
    </div>
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
  const examples = [
    "US Senators from the Northeast",
    "Papers by David Autor",
    `Leading ${kind.toLowerCase()}s in this market`,
  ];
  return (
    <Modal
      title="Find rows with AI"
      subtitle={`Describe which ${kind.toLowerCase()} entities belong in this table. An agent will research candidates; you approve them before anything is added.`}
      onClose={onClose}
    >
      <label>
        Describe the rows you want
        <textarea
          autoFocus
          value={instructions}
          onChange={(event) => setInstructions(event.target.value)}
          placeholder={`For example: US Senators from the Northeast`}
        />
      </label>
      <div className="entity-query-examples">
        <small>Try an example</small>
        <div>
          {examples.map((example) => (
            <button key={example} onClick={() => setInstructions(example)}>
              {example}
            </button>
          ))}
        </div>
      </div>
      <label>
        Maximum candidates
        <input
          type="number"
          min={1}
          max={20}
          value={count}
          onChange={(event) => setCount(Number(event.target.value))}
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
          disabled={count < 1 || count > 20 || !instructions.trim()}
          onClick={() => void onLaunch(count, instructions)}
        >
          Add to Control Center →
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
          Add to Control Center →
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

function CenterRowView({
  row,
  questions,
  onInspect,
}: {
  row: Matrix["rows"][number];
  questions: Question[];
  onInspect: (question: Question, cell: Cell) => void;
}) {
  const imageQuestion = questions.find((question) => question.value_type === "Image");
  const imageCell = imageQuestion ? row.cells[imageQuestion.name] : undefined;
  const imageUrl =
    imageCell?.state === "Answered" && typeof imageCell.value === "string"
      ? imageCell.value
      : null;
  return (
    <section className="center-row-view">
      <header>
        {imageUrl && <img src={imageUrl} alt={row.name} referrerPolicy="no-referrer" />}
        <div><span>ROW</span><h2>{row.name}</h2></div>
      </header>
      <div className="center-row-fields">
        {questions.map((question) => {
          const cell = row.cells[question.name];
          return (
            <button key={question.question_id} onClick={() => onInspect(question, cell)}>
              <span><b>{String(question.definition.label ?? question.name.replaceAll("_", " "))}</b><small>{question.value_type}</small></span>
              <span>
                {question.value_type === "Image" && typeof cell.value === "string" ? (
                  <img src={cell.value} alt="" referrerPolicy="no-referrer" />
                ) : cell.references?.length ? (
                  <><strong>{cell.references.map((item) => item.name).join(", ")}</strong><small>↗ {cell.references.map((item) => item.entity_id).join(", ")}</small></>
                ) : <strong>{cellDisplay(cell, question)}</strong>}
                <small>{cell.state}{cell.lineage.some((item) => item.derivation) ? " · ƒ derived" : ""}{cell.lineage.length ? ` · ${cell.lineage.length} src` : ""}</small>
              </span>
            </button>
          );
        })}
      </div>
    </section>
  );
}

function EntityRelationshipsDrawer({
  selection,
  row,
  questions,
  showFields,
  onClose,
  onMerge,
  onNavigate,
  onInspect,
}: {
  selection: EntitySelection;
  row: Matrix["rows"][number];
  questions: Question[];
  showFields: boolean;
  onClose: () => void;
  onMerge: () => void;
  onNavigate: (entity: RelatedEntity) => void;
  onInspect: (question: Question, cell: Cell) => void;
}) {
  const [graph, setGraph] = useState<RelationshipGraph | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    setGraph(null);
    setError("");
    api<RelationshipGraph>(
      `/api/related/${encodeURIComponent(selection.entityId)}?direction=both&depth=1`,
    )
      .then((result) => {
        if (!cancelled) setGraph(result);
      })
      .catch((caught) => {
        if (!cancelled)
          setError(
            caught instanceof Error
              ? caught.message
              : "Could not load references",
          );
      });
    return () => {
      cancelled = true;
    };
  }, [selection.entityId]);
  const incoming =
    graph?.edges.filter((edge) => edge.direction === "incoming") ?? [];
  const outgoing =
    graph?.edges.filter((edge) => edge.direction === "outgoing") ?? [];
  const groups = incoming.reduce(
    (result, edge) => {
      const key = `${edge.from.kind}:${edge.question}`;
      const group = result.get(key) ?? {
        kind: edge.from.kind,
        question: edge.question,
        edges: [],
      };
      group.edges.push(edge);
      result.set(key, group);
      return result;
    },
    new Map<
      string,
      {
        kind: string;
        question: string;
        edges: RelationshipGraph["edges"];
      }
    >(),
  );
  const imageQuestion = questions.find((question) => question.value_type === "Image");
  const imageCell = imageQuestion ? row.cells[imageQuestion.name] : undefined;
  const imageUrl =
    imageCell?.state === "Answered" && typeof imageCell.value === "string"
      ? imageCell.value
      : null;
  return (
    <aside className="drawer entity-relationships-drawer">
      <div className="drawer-head">
        <div className="eyebrow inspector-eyebrow">
          INSPECTOR <span>ROW</span>
        </div>
        <button className="close" onClick={onClose}>
          ×
        </button>
        <div className="row-detail-heading">
          {imageUrl && (
            <img
              src={imageUrl}
              alt={selection.entityName}
              loading="eager"
              referrerPolicy="no-referrer"
            />
          )}
          <div>
            <h2>{selection.entityName}</h2>
            <p>{selection.entityKind} · row details</p>
          </div>
        </div>
      </div>
      <div className="drawer-body">
        {showFields && <section className="row-detail-table">
          {questions.map((question) => {
            const cell = row.cells[question.name];
            return (
              <button
                type="button"
                className="row-detail-field"
                key={question.question_id}
                onClick={() => onInspect(question, cell)}
              >
                <span>
                  <b>
                    {String(
                      question.definition.label ?? question.name.replaceAll("_", " "),
                    )}
                  </b>
                  <small>{question.value_type}</small>
                </span>
                <span className="row-detail-value">
                  {question.value_type === "Image" &&
                  cell.state === "Answered" &&
                  typeof cell.value === "string" ? (
                    <img
                      className="row-detail-image"
                      src={cell.value}
                      alt={`${selection.entityName} · ${question.name}`}
                      loading="lazy"
                      referrerPolicy="no-referrer"
                    />
                  ) : cell.references?.length ? (
                    <>
                      <strong>
                        {cell.references.map((reference) => reference.name).join(", ")}
                      </strong>
                      <small>
                        ↗ {cell.references.map((reference) => reference.entity_id).join(", ")}
                      </small>
                    </>
                  ) : (
                    <strong>{cellDisplay(cell, question)}</strong>
                  )}
                  <small>
                    {cell.state}
                    {cell.lineage.some((item) => item.derivation) ? " · ƒ derived" : ""}
                    {cell.lineage.length ? ` · ${cell.lineage.length} src` : ""}
                  </small>
                </span>
              </button>
            );
          })}
        </section>}
        {error && <div className="form-error">{error}</div>}
        {!graph && !error && (
          <div className="center">
            <span className="spinner" />
            Loading references…
          </div>
        )}
        {graph && (
          <>
            <section className="back-reference-section">
              <div className="proposal-heading">
                <b>Back-references</b>
                <small>
                  {incoming.length} row{incoming.length === 1 ? "" : "s"} point
                  here.
                </small>
              </div>
              {incoming.length === 0 && (
                <div className="drawer-empty compact">
                  <div>↩</div>
                  <p>No rows currently reference this row.</p>
                </div>
              )}
              {[...groups.entries()].map(([key, group]) => (
                <div className="back-reference-group" key={key}>
                  <div>
                    <b>{group.kind}</b>
                    <code>via {group.question}</code>
                  </div>
                  {group.edges.map((edge) => (
                    <button
                      key={`${edge.from.entity_id}:${edge.question}`}
                      onClick={() => onNavigate(edge.from)}
                    >
                      <span>{edge.from.name}</span>
                      <small>Open in {edge.from.kind} →</small>
                    </button>
                  ))}
                </div>
              ))}
            </section>
            <section className="outgoing-reference-section">
              <div className="proposal-heading">
                <b>Outgoing references</b>
                <small>
                  {outgoing.length} related row
                  {outgoing.length === 1 ? "" : "s"}.
                </small>
              </div>
              {outgoing.map((edge) => (
                <button
                  className="outgoing-reference"
                  key={`${edge.to.entity_id}:${edge.question}`}
                  onClick={() => onNavigate(edge.to)}
                >
                  <span>
                    <code>{edge.question}</code>
                    {edge.to.name}
                  </span>
                  <small>{edge.to.kind} →</small>
                </button>
              ))}
            </section>
            <section className="entity-correction-section">
              <div>
                <b>Row identity</b>
                <small>Correct duplicate identities without erasing history.</small>
              </div>
              <button onClick={onMerge}>Merge duplicate…</button>
            </section>
          </>
        )}
      </div>
    </aside>
  );
}

function MergeEntityDialog({
  source,
  rows,
  questions,
  onClose,
  onSaved,
}: {
  source: EntitySelection;
  rows: Matrix["rows"];
  questions: Question[];
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const [destinationId, setDestinationId] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const sourceRow = rows.find((row) => row.entity_id === source.entityId);
  const destination = rows.find((row) => row.entity_id === destinationId);
  const populated = (row: Matrix["rows"][number] | undefined) =>
    row
      ? questions.filter((question) => row.cells[question.name]?.state !== "Unasked")
          .length
      : 0;
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!destinationId || !reason.trim() || saving) return;
    setSaving(true);
    setError("");
    try {
      await post(`/api/entities/${encodeURIComponent(source.entityId)}/merge`, {
        destination: destinationId,
        reason,
      });
      await onSaved();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not merge rows");
      setSaving(false);
    }
  };
  return (
    <Modal
      title={`Merge duplicate “${source.entityName}”`}
      subtitle="Choose the row that represents the surviving identity. Epiq records the correction instead of deleting history."
      onClose={onClose}
    >
      <form onSubmit={(event) => void submit(event)}>
        <label>
          Merge into
          <select
            value={destinationId}
            onChange={(event) => setDestinationId(event.target.value)}
            autoFocus
            required
          >
            <option value="">Choose the surviving {source.entityKind} row…</option>
            {rows
              .filter((row) => row.entity_id !== source.entityId)
              .map((row) => (
                <option key={row.entity_id} value={row.entity_id}>
                  {row.name}
                </option>
              ))}
          </select>
        </label>
        {destination && (
          <div className="merge-preview">
            <div>
              <small>Duplicate</small>
              <b>{source.entityName}</b>
              <span>{populated(sourceRow)} investigated field{populated(sourceRow) === 1 ? "" : "s"}</span>
            </div>
            <strong>→</strong>
            <div>
              <small>Surviving row</small>
              <b>{destination.name}</b>
              <span>{populated(destination)} investigated field{populated(destination) === 1 ? "" : "s"}</span>
            </div>
          </div>
        )}
        <div className="merge-explanation">
          <b>What happens</b>
          <ul>
            <li>The duplicate row disappears from the current table.</li>
            <li>Its claims, evidence, references, name, and ID resolve to the survivor.</li>
            <li>Conflicting answers are retained and surfaced for review.</li>
          </ul>
        </div>
        <label>
          Why are these the same entity?
          <textarea
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder={`For example: “${source.entityName}” is an alternate name for the same theater.`}
            required
          />
        </label>
        {error && <div className="form-error">{error}</div>}
        <div className="modal-actions">
          <button type="button" className="ghost" onClick={onClose}>Cancel</button>
          <button className="danger-button" disabled={saving || !destinationId || !reason.trim()}>
            {saving ? "Merging…" : "Merge rows"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

type DuplicateCandidate = {
  candidate_id: string;
  score: number;
  duplicate: { entity_id: string; name: string; investigated_fields: number };
  survivor: { entity_id: string; name: string; investigated_fields: number };
  reasons: string[];
  conflicting_fields: number;
};

function DuplicateReviewDialog({
  kind,
  onClose,
  onChanged,
}: {
  kind: string;
  onClose: () => void;
  onChanged: () => Promise<void>;
}) {
  const [candidates, setCandidates] = useState<DuplicateCandidate[] | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    api<{ candidates: DuplicateCandidate[] }>(
      `/api/entities/duplicate-candidates?kind=${encodeURIComponent(kind)}`,
    )
      .then((result) => setCandidates(result.candidates))
      .catch((caught) =>
        setError(caught instanceof Error ? caught.message : "Could not scan for duplicates"),
      );
  }, [kind]);
  const dismiss = (candidateId: string) =>
    setCandidates((current) => current?.filter((item) => item.candidate_id !== candidateId) ?? []);
  const swap = (candidateId: string) =>
    setCandidates((current) =>
      current?.map((item) =>
        item.candidate_id === candidateId
          ? { ...item, duplicate: item.survivor, survivor: item.duplicate }
          : item,
      ) ?? [],
    );
  const merge = async (candidate: DuplicateCandidate) => {
    setBusy(candidate.candidate_id);
    setError("");
    try {
      await post(
        `/api/entities/${encodeURIComponent(candidate.duplicate.entity_id)}/merge`,
        {
          destination: candidate.survivor.entity_id,
          reason: `Human-reviewed duplicate: “${candidate.duplicate.name}” and “${candidate.survivor.name}” represent the same ${kind} entity.`,
        },
      );
      setCandidates((current) =>
        current?.filter(
          (item) =>
            item.candidate_id !== candidate.candidate_id &&
            item.duplicate.entity_id !== candidate.duplicate.entity_id &&
            item.survivor.entity_id !== candidate.duplicate.entity_id,
        ) ?? [],
      );
      await onChanged();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not merge duplicate");
    } finally {
      setBusy("");
    }
  };
  return (
    <Modal
      title={`Find duplicate ${kind} rows`}
      subtitle="Epiq compares row names and investigated values. Nothing is merged without your confirmation."
      onClose={onClose}
    >
      {!candidates && !error && <div className="center"><span className="spinner" />Scanning rows…</div>}
      {error && <div className="form-error">{error}</div>}
      {candidates?.length === 0 && (
        <div className="duplicate-empty"><b>No more likely duplicates</b><p>The automatic scan found no additional high-similarity pairs.</p></div>
      )}
      <div className="duplicate-review-list">
        {candidates?.map((candidate) => (
          <article key={candidate.candidate_id} className="duplicate-review-card">
            <div className="duplicate-score">{Math.round(candidate.score * 100)}% match</div>
            <div className="duplicate-pair">
              <div><small>Merge duplicate</small><b>{candidate.duplicate.name}</b><span>{candidate.duplicate.investigated_fields} investigated fields</span></div>
              <strong>→</strong>
              <div><small>Into survivor</small><b>{candidate.survivor.name}</b><span>{candidate.survivor.investigated_fields} investigated fields</span></div>
            </div>
            <ul>{candidate.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>
            {candidate.conflicting_fields > 0 && <p className="duplicate-conflict">⚠ {candidate.conflicting_fields} field{candidate.conflicting_fields === 1 ? "" : "s"} conflict and will be retained for review.</p>}
            <div className="duplicate-review-actions">
              <button onClick={() => dismiss(candidate.candidate_id)}>Not duplicates</button>
              <button onClick={() => swap(candidate.candidate_id)}>Swap survivor</button>
              <button className="primary" disabled={busy === candidate.candidate_id} onClick={() => void merge(candidate)}>{busy === candidate.candidate_id ? "Merging…" : "Confirm merge"}</button>
            </div>
          </article>
        ))}
      </div>
      <div className="modal-actions"><button className="primary" onClick={onClose}>Done</button></div>
    </Modal>
  );
}

function CellDrawer({
  selection,
  provisionalRelationships,
  staleDerivedClaimIds,
  isResearching,
  onClose,
  onAdd,
  onNotFound,
  onEnrich,
  onResearch,
  onCancelResearch,
  onPolicy,
  onSchemaChallenge,
  onChallengeResearch,
  onEstimate,
  onChallenge,
  onAcceptProvisional,
  onAcceptAllProvisional,
  onRejectAllProvisional,
  onChanged,
  onReviewed,
}: {
  selection: Selection;
  provisionalRelationships: ProvisionalRelationship[];
  staleDerivedClaimIds: Set<string>;
  isResearching: boolean;
  onClose: () => void;
  onAdd: () => void;
  onNotFound: () => void;
  onEnrich: () => void;
  onResearch: () => void;
  onCancelResearch: () => Promise<void>;
  onPolicy: () => void;
  onSchemaChallenge: () => void;
  onChallengeResearch: () => void;
  onEstimate: () => void;
  onChallenge: (claimId: string) => void;
  onAcceptProvisional: (suggestion: ProvisionalRelationship) => Promise<void>;
  onAcceptAllProvisional: (
    suggestions: ProvisionalRelationship[],
  ) => Promise<void>;
  onRejectAllProvisional: (
    suggestions: ProvisionalRelationship[],
  ) => Promise<void>;
  onChanged: () => Promise<void>;
  onReviewed: () => Promise<void>;
}) {
  const { cell, entityName, question } = selection;
  const [busy, setBusy] = useState("");
  const [reviewError, setReviewError] = useState("");
  const reviewClaim = async (claimId: string) => {
    const reason = window.prompt("What did you verify against the source?");
    if (!reason?.trim()) return;
    setBusy(claimId);
    setReviewError("");
    try {
      await post(`/api/claims/${claimId}/review`, { decision: "accepted", reason });
      await onReviewed();
    } catch (error) {
      setReviewError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy("");
    }
  };
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
        <div className="eyebrow inspector-eyebrow">
          INSPECTOR <span>CELL</span>
        </div>
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
            <b>{isResearching ? "Researching" : cellStateLabel(cell)}</b>
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
        {provisionalRelationships.length > 0 && (
          <section className="provisional-relationships">
            <div className="proposal-heading">
              <span>
                <b>Provisional related rows</b>
                <small>
                  Agent findings—not part of the database until approved.
                </small>
              </span>
              <div className="provisional-heading-actions">
                <button
                  disabled={Boolean(busy)}
                  onClick={() =>
                    void onRejectAllProvisional(provisionalRelationships)
                  }
                >
                  Reject all
                </button>
                <button
                  className="accept-all-provisional"
                  disabled={Boolean(busy)}
                  onClick={async () => {
                    setBusy("all");
                    try {
                      await onAcceptAllProvisional(provisionalRelationships);
                    } finally {
                      setBusy("");
                    }
                  }}
                >
                  {busy === "all"
                    ? "Accepting…"
                    : `Accept all ${provisionalRelationships.length}`}
                </button>
              </div>
            </div>
            {provisionalRelationships.map((suggestion) => (
              <article
                className="provisional-relationship-card"
                key={suggestion.suggestion_id}
              >
                <div>
                  <span className="provisional-badge">PROVISIONAL</span>
                  <b>{suggestion.target_name}</b>
                  <small>
                    {suggestion.action === "link"
                      ? `Links an existing ${suggestion.target_kind} row`
                      : `Creates a ${suggestion.target_kind} row and links it`}
                  </small>
                </div>
                <blockquote>{suggestion.excerpt}</blockquote>
                {suggestion.source_url ? (
                  <a
                    href={suggestion.source_url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    ↗ {suggestion.source_title}
                  </a>
                ) : (
                  <small>{suggestion.source_title}</small>
                )}
                <button
                  className="primary"
                  disabled={Boolean(busy)}
                  onClick={async () => {
                    setBusy(suggestion.suggestion_id);
                    try {
                      await onAcceptProvisional(suggestion);
                    } finally {
                      setBusy("");
                    }
                  }}
                >
                  {busy === suggestion.suggestion_id
                    ? "Accepting…"
                    : `Accept ${suggestion.target_name}`}
                </button>
              </article>
            ))}
          </section>
        )}
        {cell.state === "NotFound" && (
          <div className="research-card">
            <small>SEARCHED FOR</small>
            <code>{cell.research?.query}</code>
            <p>{cell.research?.notes}</p>
            <button className="challenge-outcome" onClick={onChallengeResearch}>
              Discuss and retry in Control Center
            </button>
          </div>
        )}
        {claims.map(({ claim, evidence }) => (
          <article
            className={`claim-card ${staleDerivedClaimIds.has(claim.claim_id) ? "stale-derived-claim" : ""}`}
            key={claim.claim_id}
          >
            <div className="claim-top">
              <span className={`confidence ${claim.confidence}`}>
                {claim.confidence}
              </span>
              {claim.epistemic_status === "estimated" && (
                <span className="estimate-badge">≈ estimated</span>
              )}
              <code>{claim.token}</code>
            </div>
            <div className="answer">
              {question.value_type === "URL" &&
              typeof claim.value === "string" ? (
                <a
                  className="answer-url"
                  href={claim.value}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {claim.value} ↗
                </a>
              ) : question.value_type.startsWith("Ref[") &&
                typeof claim.value === "string" ? (
                <span className="relationship-value">
                  ↗{" "}
                  {cell.references?.find(
                    (reference) => reference.entity_id === claim.value,
                  )?.name ?? claim.value}
                </span>
              ) : (
                formattedValue(claim.value, question)
              )}
            </div>
            {claim.as_of && (
              <div className="claim-as-of">
                Claim observed as of {claim.as_of}
              </div>
            )}
            {claim.epistemic_status === "estimated" && claim.estimate && (
              <div className="estimate-panel">
                <b>Ballpark estimate</b>
                {(claim.estimate.lower != null || claim.estimate.upper != null) && (
                  <p>
                    Plausible range: {String(claim.estimate.lower ?? "?")}–
                    {String(claim.estimate.upper ?? "?")}
                    {question.value_type.startsWith("Quantity[")
                      ? ` ${question.value_type.slice("Quantity[".length, -1)}`
                      : ""}
                  </p>
                )}
                {claim.estimate.method && <p>{claim.estimate.method}</p>}
                {claim.estimate.proxy && <small>Proxy: {claim.estimate.proxy}</small>}
                {claim.estimate.assumptions?.length ? (
                  <ul>{claim.estimate.assumptions.map((item) => <li key={item}>{item}</li>)}</ul>
                ) : null}
              </div>
            )}
            {claim.derivation && (
              <div className="derivation-panel">
                <div>
                  <b>ƒ Derived value</b>
                  <code>{claim.derivation.operation}</code>
                </div>
                {staleDerivedClaimIds.has(claim.claim_id) && (
                  <p className="derivation-stale-message">
                    ⚠ One or more dependencies changed. Recalculate before
                    treating this as current.
                  </p>
                )}
                <ul>
                  {claim.derivation.dependencies.map((dependency) => (
                    <li key={`${dependency.role}:${dependency.claim_id}`}>
                      <span>{dependency.role}</span>
                      <code>{dependency.claim_id}</code>
                    </li>
                  ))}
                </ul>
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
                  <small className="source-assurance">
                    {item.provenance?.capture_method ?? "legacy"} source · {" "}
                    {item.provenance?.verification_status === "excerpt_matched" ? "excerpt matched" : "capture unverified"}
                    {" · "}{item.support?.length ? "support span recorded" : "no support span"}
                    {" · "}{item.review?.status === "accepted" ? `reviewed by ${item.review.actor}` : item.review?.status === "rejected" ? "review rejected" : "unreviewed"}
                  </small>
                  {item.provenance?.content_hash && (
                    <details className="capture-details">
                      <summary>Capture details</summary>
                      <small>{item.provenance.provider} · {item.provenance.captured_at}</small>
                      <code>{item.provenance.content_hash}</code>
                    </details>
                  )}
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
              {claim.review?.status !== "accepted" && (
                <button disabled={busy === claim.claim_id} onClick={() => void reviewClaim(claim.claim_id)}>
                  Mark reviewed
                </button>
              )}
              {reviewError && <span role="alert">{reviewError}</span>}
              <button onClick={() => onChallenge(claim.claim_id)}>
                Discuss in Control Center
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
        {cell.state === "Unasked" && provisionalRelationships.length === 0 && (
          <div className="drawer-empty">
            <div>?</div>
            <p>No answer or completed research has been recorded.</p>
          </div>
        )}
      </div>
      <div className="drawer-actions">
        {isResearching ? (
          <button className="danger-button" onClick={onCancelResearch}>
            Stop research
          </button>
        ) : cell.state === "Answered" ? (
          <button className="ghost" onClick={onEnrich}>
            ✦ Get more evidence
          </button>
        ) : cell.state === "NotFound" ? (
          <>
            <button className="primary" onClick={onEstimate}>
              Estimate a ballpark value
            </button>
            <button className="ghost" onClick={onChallengeResearch}>
              Retry exact research
            </button>
          </>
        ) : (
          <button className="primary" onClick={onResearch}>
            Ask Control Center to research
          </button>
        )}
      </div>
    </aside>
  );
}
