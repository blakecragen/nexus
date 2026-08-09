/**
 * JobBuilder.tsx — visual pipeline composer (route `/jobs/new`).
 *
 * Role in the system:
 *   The only place a job is created. It presents the server's registered step
 *   types as a searchable palette, lets the user assemble an ordered list of
 *   steps with per-step parameters, choose a target (pool or specific node) and
 *   a priority, then POSTs the whole thing to /api/jobs.
 *
 * Data flow:
 *   - `useStepsStore`  -> GET /api/steps — the step *schemas* (name,
 *     description, supported OS, `requires_node`, and a `fields` array that
 *     drives the dynamically generated parameter form).
 *   - `usePoolsStore`  -> GET /api/pools  (target dropdown)
 *   - `useNodesStore`  -> GET /api/nodes  (target dropdown, online only)
 *   - `api.submitJob`  -> POST /api/jobs, then navigates to
 *     `JobDetail.tsx` at `/jobs/{id}`.
 *
 * AI Note: the parameter form is entirely schema-driven — the frontend hard-codes
 * no knowledge of any specific step. Adding a step type on the server makes it
 * appear here automatically, provided its prefix is in {@link STEP_CATEGORIES}
 * (otherwise it lands in "Other") and its field types are handled by
 * {@link FieldInput} (otherwise they render as text inputs).
 *
 * AI Note: submit-time validation lives on the SERVER (it walks the step list
 * accumulating each step's OUTPUT_KEYS so chained steps can reference upstream
 * outputs). This page only checks "has a name" and "has at least one step" —
 * do not duplicate the dependency-graph validation here, it will drift.
 *
 * Layout: three columns — palette (left), drag-and-drop canvas (centre),
 * parameter editor + submit form (right). Drag-and-drop uses @dnd-kit for
 * reordering within the canvas, and the native HTML5 drag API for palette ->
 * canvas.
 */
import { useState, useEffect, useMemo, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import {
  DndContext,
  DragOverlay,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type DragStartEvent,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  sortableKeyboardCoordinates,
  verticalListSortingStrategy,
  useSortable,
  arrayMove,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import {
  Search,
  GripVertical,
  X,
  Play,
  Package,
  AlertCircle,
  Layers,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/api/client";
import { useStepsStore, usePoolsStore, useNodesStore } from "@/stores";
import type { StepSchemaInfo, FieldSchema } from "@/types";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/**
 * One step as held in the builder's local state.
 *
 * AI Note: `id` is a client-only handle used as the React key and the dnd-kit
 * sortable id — it is NOT sent to the server and has no relationship to the
 * `StepRun.id` that comes back on the job detail page. `step` is the schema
 * name; `params` is the raw form state (which can contain empty strings that
 * are stripped at submit time).
 */
interface BuilderStep {
  id: string;
  step: string;
  params: Record<string, unknown>;
}

/** Whether the job targets a whole pool or one specific node. */
type TargetMode = "pool" | "node";
/** User-facing priority labels; mapped to numbers by {@link PRIORITY_VALUES}. */
type Priority = "high" | "normal" | "low";

/**
 * Label -> numeric priority sent as `priority` in the submit payload.
 *
 * AI Note: the server's scheduler orders the queue by this integer, HIGHER
 * first. The 10/5/1 spacing is deliberate so intermediate values can be
 * introduced later (or set via the API) without renumbering these three.
 */
const PRIORITY_VALUES: Record<Priority, number> = {
  high: 10,
  normal: 5,
  low: 1,
};

// ---------------------------------------------------------------------------
// Category helpers
// ---------------------------------------------------------------------------

/**
 * Palette section headings, keyed by the step name's underscore-delimited
 * prefix (`shell_run` -> `shell` -> "Shell").
 *
 * AI Note: this is a display-only convention with no server counterpart — the
 * backend does not tag steps with a category. A new step whose prefix is
 * missing here silently lands in the "Other" bucket, which is a common cause of
 * "my new step isn't showing up where I expected".
 */
const STEP_CATEGORIES: Record<string, string> = {
  shell: "Shell",
  flow: "Flow Control",
  git: "Git",
  gem5: "gem5",
  package: "Package",
  system: "System",
};

/**
 * Maps a step name to its palette category via its prefix.
 *
 * @param stepName e.g. `"gem5_collect_results"`.
 * @returns the display category, or `"Other"` for unknown prefixes. A name with
 *   no underscore uses the whole name as the prefix.
 */
function categorize(stepName: string): string {
  const prefix = stepName.split("_")[0]?.toLowerCase() ?? "";
  return STEP_CATEGORIES[prefix] ?? "Other";
}

/**
 * Buckets step schemas into palette sections.
 *
 * @param steps the (already search-filtered) schema list.
 * @returns an object whose keys are categories in display order.
 *
 * AI Note: relies on JS object key insertion order to control the rendered
 * section order — categories are inserted alphabetically with "Other" forced
 * last. Do not swap this for a plain object literal or a `Map`-to-object
 * conversion that loses that ordering.
 */
function groupSteps(
  steps: StepSchemaInfo[]
): Record<string, StepSchemaInfo[]> {
  const groups: Record<string, StepSchemaInfo[]> = {};
  for (const s of steps) {
    const cat = categorize(s.name);
    (groups[cat] ??= []).push(s);
  }
  // Sort keys with "Other" last
  const sorted: Record<string, StepSchemaInfo[]> = {};
  for (const key of Object.keys(groups).sort((a, b) =>
    a === "Other" ? 1 : b === "Other" ? -1 : a.localeCompare(b)
  )) {
    sorted[key] = groups[key]!;
  }
  return sorted;
}

// ---------------------------------------------------------------------------
// Build default params from schema
// ---------------------------------------------------------------------------

/**
 * Seeds a new step's parameter object from its schema.
 *
 * @param schema the step schema fetched from GET /api/steps.
 * @returns every declared field pre-populated: the schema default when there is
 *   one, otherwise `false` for booleans and `""` for everything else.
 *
 * AI Note: every field is initialised (never left undefined) so the form inputs
 * are controlled from the first render — React warns loudly when an input flips
 * from uncontrolled to controlled. The empty-string placeholders are stripped
 * again in `handleSubmit`, so "" never reaches the API. That also means a step
 * whose legitimate value IS an empty string cannot express it through this UI.
 */
function buildDefaultParams(schema: StepSchemaInfo): Record<string, unknown> {
  const params: Record<string, unknown> = {};
  for (const f of schema.fields) {
    if (f.default !== null && f.default !== undefined) {
      params[f.name] = f.default;
    } else if (f.field_type === "boolean") {
      params[f.name] = false;
    } else {
      params[f.name] = "";
    }
  }
  return params;
}

// ---------------------------------------------------------------------------
// Unique ID helper
// ---------------------------------------------------------------------------

/** Monotonic counter backing {@link uniqueId}; module-scoped so it survives
 * component remounts within a session. */
let _idCounter = 0;

/**
 * Generates a client-side id for a canvas step.
 *
 * AI Note: `Date.now()` alone is not sufficient — two steps added in the same
 * millisecond (easy when clicking fast, or when a future "duplicate" action is
 * added) would collide, and duplicate ids break both React keys and dnd-kit's
 * sortable identity in confusing ways. The counter guarantees uniqueness; the
 * timestamp is only there to keep ids readable/ordered.
 */
function uniqueId(): string {
  return `step-${Date.now()}-${++_idCounter}`;
}

// ---------------------------------------------------------------------------
// OS badge color
// ---------------------------------------------------------------------------

/**
 * Tailwind classes for the small OS chips on a palette item.
 *
 * @param os an entry from the schema's `supported_os` array.
 * @returns a class pair; unknown values get neutral styling.
 */
function osBadgeClass(os: string): string {
  switch (os.toLowerCase()) {
    case "macos":
      return "bg-secondary text-foreground";
    case "linux":
      return "bg-amber-50 text-amber-700";
    case "windows":
      return "bg-blue-50 text-blue-700";
    default:
      return "bg-secondary text-muted-foreground";
  }
}

// ---------------------------------------------------------------------------
// Palette Step Item (drag source)
// ---------------------------------------------------------------------------

/**
 * One entry in the left-hand step palette.
 *
 * What the user sees: a card with the step name, a two-line-clamped
 * description, and a chip per supported OS. It can be clicked to append the
 * step, or dragged onto the canvas.
 *
 * @param schema the step schema to display.
 * @param onAdd invoked on click to append this step to the canvas.
 *
 * AI Note: this uses the NATIVE HTML5 drag API (`draggable` +
 * `dataTransfer`), while canvas reordering uses @dnd-kit. Two separate drag
 * systems coexist on this page: palette -> canvas is native (a "copy"
 * operation carrying the step name), canvas -> canvas is dnd-kit (a "move" of
 * an existing id). The `application/nexus-step` MIME type is the private
 * contract between this component and `handleCanvasDrop`; changing it in one
 * place silently breaks drag-to-add.
 */
function PaletteItem({
  schema,
  onAdd,
}: {
  schema: StepSchemaInfo;
  onAdd: (schema: StepSchemaInfo) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onAdd(schema)}
      className={cn(
        "w-full text-left p-3 rounded-lg border border-border bg-card",
        "hover:border-indigo-300 hover:shadow-sm transition-all cursor-grab",
        "active:cursor-grabbing"
      )}
      draggable
      onDragStart={(e) => {
        e.dataTransfer.setData("application/nexus-step", schema.name);
        e.dataTransfer.effectAllowed = "copy";
      }}
    >
      <div className="font-medium text-sm text-foreground truncate">
        {schema.name}
      </div>
      {schema.description && (
        <div className="text-xs text-muted-foreground mt-0.5 line-clamp-2">
          {schema.description}
        </div>
      )}
      <div className="flex flex-wrap gap-1 mt-1.5">
        {schema.supported_os.map((os) => (
          <span
            key={os}
            className={cn(
              "inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium",
              osBadgeClass(os)
            )}
          >
            {os}
          </span>
        ))}
      </div>
    </button>
  );
}

// ---------------------------------------------------------------------------
// Sortable Canvas Card
// ---------------------------------------------------------------------------

/**
 * A step card on the canvas: selectable, reorderable, removable.
 *
 * What the user sees: a drag handle, the 1-based position number, the step
 * name, a short summary of the first few non-empty params, an optional "no
 * node" chip, and an X to remove it. The selected card gets an indigo ring.
 *
 * @param bstep the builder step this card represents.
 * @param index 0-based position; displayed as `index + 1`.
 * @param schema the matching schema, or undefined if the server no longer
 *   advertises this step (the card still renders, minus the "no node" chip).
 * @param isSelected whether this card's params are in the right-hand editor.
 * @param onSelect selects this card for editing.
 * @param onRemove deletes it from the canvas.
 *
 * AI Note: dnd-kit's `listeners`/`attributes` are spread onto the grip button
 * ONLY, not the card root. That is what allows the whole card to stay
 * clickable for selection — putting them on the root would make every click
 * start a drag. The `touch-none` class on the handle stops mobile scroll from
 * hijacking the gesture.
 */
function SortableStepCard({
  bstep,
  index,
  schema,
  isSelected,
  onSelect,
  onRemove,
}: {
  bstep: BuilderStep;
  index: number;
  schema: StepSchemaInfo | undefined;
  isSelected: boolean;
  onSelect: () => void;
  onRemove: () => void;
}) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: bstep.id });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
  };

  // Brief param summary
  //
  // AI Note: the filter drops `false` as well as empty values, so an
  // explicitly-disabled boolean flag never appears in the summary line — it
  // reads as "unset". Values are truncated at 30 chars and only the first 3
  // params are shown; this is a preview, never a source of truth.
  const paramSummary = Object.entries(bstep.params)
    .filter(([, v]) => v !== "" && v !== false && v !== null && v !== undefined)
    .map(([k, v]) => {
      const val = typeof v === "string" && v.length > 30 ? v.slice(0, 30) + "..." : String(v);
      return `${k}=${val}`;
    })
    .slice(0, 3)
    .join(", ");

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={cn(
        "flex items-start gap-2 p-3 rounded-lg border bg-card transition-all",
        isDragging && "opacity-40",
        isSelected
          ? "ring-2 ring-indigo-500 border-indigo-300 shadow-md"
          : "border-border shadow-sm hover:shadow"
      )}
      onClick={onSelect}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
    >
      {/* Drag handle */}
      <button
        type="button"
        className="mt-0.5 text-muted-foreground hover:text-muted-foreground cursor-grab active:cursor-grabbing touch-none"
        {...attributes}
        {...listeners}
      >
        <GripVertical className="h-4 w-4" />
      </button>

      {/* Step number */}
      <span className="flex-shrink-0 w-6 h-6 rounded-full bg-indigo-50 text-indigo-600 text-xs font-semibold flex items-center justify-center mt-0.5">
        {index + 1}
      </span>

      {/* Content */}
      <div className="flex-1 min-w-0">
        <div className="font-medium text-sm text-foreground">{bstep.step}</div>
        {paramSummary && (
          <div className="text-xs text-muted-foreground mt-0.5 truncate">
            {paramSummary}
          </div>
        )}
        {/* AI Note: `requires_node === false` means the step runs on the
            server itself rather than being dispatched to an agent (e.g. flow
            control). The chip is a scheduling hint for the user — such a step
            will execute even if no node is available. */}
        {schema && !schema.requires_node && (
          <span className="inline-flex items-center mt-1 px-1.5 py-0.5 rounded text-[10px] font-medium bg-green-50 text-green-600">
            no node
          </span>
        )}
      </div>

      {/* Remove */}
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          onRemove();
        }}
        className="flex-shrink-0 text-muted-foreground hover:text-red-500 transition-colors mt-0.5"
        title="Remove step"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Drag Overlay Card (follows cursor during drag)
// ---------------------------------------------------------------------------

/**
 * The simplified card that follows the cursor while a canvas step is being
 * dragged (rendered inside dnd-kit's `<DragOverlay>`).
 *
 * Intentionally a stripped-down copy of {@link SortableStepCard} — no params
 * summary, no remove button, no sortable wiring — because the overlay is
 * detached from the list and must not be interactive.
 *
 * @param bstep the step being dragged.
 * @param index its position at drag start; the number does NOT update as the
 *   item is dragged past others.
 */
function DragOverlayCard({
  bstep,
  index,
}: {
  bstep: BuilderStep;
  index: number;
}) {
  return (
    <div className="flex items-start gap-2 p-3 rounded-lg border border-indigo-300 bg-card shadow-lg opacity-90">
      <GripVertical className="h-4 w-4 text-muted-foreground mt-0.5" />
      <span className="flex-shrink-0 w-6 h-6 rounded-full bg-indigo-50 text-indigo-600 text-xs font-semibold flex items-center justify-center mt-0.5">
        {index + 1}
      </span>
      <div className="font-medium text-sm text-foreground">{bstep.step}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Dynamic Param Editor
// ---------------------------------------------------------------------------

/**
 * Renders the parameter form for the selected step, generated entirely from
 * its schema's `fields` array.
 *
 * @param schema the selected step's schema; `fields` drives which inputs exist.
 * @param params the current values (a controlled object owned by the page).
 * @param onChange receives a NEW params object on every keystroke — the parent
 *   replaces the whole object rather than mutating it.
 *
 * Renders an italic placeholder for steps that declare no fields.
 */
function ParamEditor({
  schema,
  params,
  onChange,
}: {
  schema: StepSchemaInfo;
  params: Record<string, unknown>;
  onChange: (params: Record<string, unknown>) => void;
}) {
  /** Immutably overwrite one field and bubble the whole params object up. */
  function updateField(name: string, value: unknown) {
    onChange({ ...params, [name]: value });
  }

  if (schema.fields.length === 0) {
    return (
      <p className="text-sm text-muted-foreground italic">
        This step has no configurable parameters.
      </p>
    );
  }

  return (
    <div className="space-y-4">
      {schema.fields.map((field) => (
        <FieldInput
          key={field.name}
          field={field}
          value={params[field.name]}
          onUpdate={(v) => updateField(field.name, v)}
        />
      ))}
    </div>
  );
}

/**
 * Renders a single schema field as the appropriate input control.
 *
 * Dispatch on `field.field_type`:
 *   - `boolean`                      -> checkbox
 *   - `integer` / `number` / `float` -> number input (coerced with `Number`)
 *   - everything else                -> text input
 *
 * @param field the schema descriptor (name, type, required, description,
 *   examples).
 * @param value current value from the params object.
 * @param onUpdate emits the new value; numbers are already coerced.
 *
 * AI Note: the fallback branch swallows any field type the server invents
 * (enums, secrets, file paths, JSON blobs) and renders it as free text. That
 * is intentional so a new backend step is never *unusable* from the UI, but it
 * means the value arrives at the API as a string. Add an explicit branch here
 * when a type needs real handling.
 *
 * AI Note: required-ness is communicated only by an asterisk in the label —
 * there is no client-side enforcement. The server rejects missing required
 * params at submit time and the error surfaces in the submit banner.
 */
function FieldInput({
  field,
  value,
  onUpdate,
}: {
  field: FieldSchema;
  value: unknown;
  onUpdate: (v: unknown) => void;
}) {
  const labelText = `${field.name}${field.required ? " *" : ""}`;

  if (field.field_type === "boolean") {
    return (
      <label className="flex items-center gap-2 cursor-pointer">
        <input
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => onUpdate(e.target.checked)}
          className="h-4 w-4 rounded border-border text-indigo-600 focus:ring-indigo-500"
        />
        <span className="text-sm font-medium text-foreground">{labelText}</span>
        {field.description && (
          <span className="text-xs text-muted-foreground ml-1">
            - {field.description}
          </span>
        )}
      </label>
    );
  }

  if (
    field.field_type === "integer" ||
    field.field_type === "number" ||
    field.field_type === "float"
  ) {
    return (
      <div>
        <label className="block text-sm font-medium text-foreground mb-1">
          {labelText}
        </label>
        {/* AI Note: an empty numeric field is stored as "" (not 0 and not
            null) so the user can clear it without the input jumping to 0;
            `handleSubmit` then strips it entirely. Coercing "" through
            `Number()` here would produce 0 and silently submit a value the
            user never entered. */}
        <input
          type="number"
          value={value === "" || value === null || value === undefined ? "" : Number(value)}
          onChange={(e) =>
            onUpdate(e.target.value === "" ? "" : Number(e.target.value))
          }
          className={cn(
            "w-full rounded-md border border-border px-3 py-2 text-sm",
            "focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 outline-none"
          )}
          placeholder={
            field.examples.length > 0 ? `e.g. ${field.examples[0]}` : undefined
          }
        />
        {field.description && (
          <p className="mt-1 text-xs text-muted-foreground">{field.description}</p>
        )}
      </div>
    );
  }

  // Default: text input (string and everything else)
  return (
    <div>
      <label className="block text-sm font-medium text-foreground mb-1">
        {labelText}
      </label>
      <input
        type="text"
        value={value == null ? "" : String(value)}
        onChange={(e) => onUpdate(e.target.value)}
        className={cn(
          "w-full rounded-md border border-border px-3 py-2 text-sm",
          "focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 outline-none"
        )}
        placeholder={
          field.examples.length > 0 ? `e.g. ${field.examples[0]}` : undefined
        }
      />
      {field.description && (
        <p className="mt-1 text-xs text-muted-foreground">{field.description}</p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// JobBuilder Page
// ---------------------------------------------------------------------------

/**
 * Job builder page.
 *
 * What the user sees: three columns.
 *   - Left: searchable, category-grouped step palette.
 *   - Centre: the ordered canvas. Empty state invites a drag; otherwise a
 *     vertical list of reorderable {@link SortableStepCard}s.
 *   - Right: the selected step's generated parameter form on top, and the job
 *     submission form (name, pool/node target, priority, submit) pinned below.
 *
 * Side effects: three GETs on mount (steps, pools, nodes); one POST /api/jobs
 * on submit, followed by navigation to the new job's detail page.
 *
 * Props: none — routed component.
 */
export default function JobBuilder() {
  const navigate = useNavigate();

  // Store data
  const stepsStore = useStepsStore();
  const poolsStore = usePoolsStore();
  const nodesStore = useNodesStore();

  // Fetch on mount
  //
  // AI Note: these components subscribe to the WHOLE store object, so
  // `stepsStore`/`poolsStore`/`nodesStore` get a new identity on every store
  // update. Listing them as deps would refetch forever, which is exactly why
  // the exhaustive-deps rule is disabled here rather than "fixed". If you
  // refactor to selector-based subscriptions (`useStepsStore(s => s.fetch)`),
  // the disable comment can go away.
  useEffect(() => {
    stepsStore.fetch();
    poolsStore.fetch();
    nodesStore.fetch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Palette search — matches step name OR description, case-insensitively.
  const [search, setSearch] = useState("");
  const filteredSteps = useMemo(() => {
    if (!search.trim()) return stepsStore.steps;
    const q = search.toLowerCase();
    return stepsStore.steps.filter(
      (s) =>
        s.name.toLowerCase().includes(q) ||
        s.description.toLowerCase().includes(q)
    );
  }, [stepsStore.steps, search]);

  const grouped = useMemo(() => groupSteps(filteredSteps), [filteredSteps]);

  // Schema lookup
  //
  // AI Note: name -> schema index built from the *unfiltered* step list, not
  // `filteredSteps`. Canvas cards and drop handling must resolve schemas even
  // for steps the current palette search has hidden.
  const schemaMap = useMemo(() => {
    const m = new Map<string, StepSchemaInfo>();
    for (const s of stepsStore.steps) m.set(s.name, s);
    return m;
  }, [stepsStore.steps]);

  // Builder state
  // `builderSteps` is the authoritative ordered pipeline; `selectedId` drives
  // the right-hand editor; `activeId` is the step currently being dragged (used
  // only to render the drag overlay).
  const [builderSteps, setBuilderSteps] = useState<BuilderStep[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);

  // Job metadata
  const [jobName, setJobName] = useState("");
  const [targetMode, setTargetMode] = useState<TargetMode>("pool");
  const [targetPoolId, setTargetPoolId] = useState("");
  const [targetNodeId, setTargetNodeId] = useState("");
  const [priority, setPriority] = useState<Priority>("normal");

  // Submit state
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // Current selected step
  // `selectedSchema` may be undefined if the server stopped advertising this
  // step type; the right-hand panel then falls back to its placeholder.
  const selectedStep = builderSteps.find((s) => s.id === selectedId);
  const selectedSchema = selectedStep
    ? schemaMap.get(selectedStep.step)
    : undefined;

  // -- Handlers --

  /** Appends a step (with schema defaults) to the end of the canvas and
   * immediately selects it so the user lands in its parameter form. */
  const addStep = useCallback(
    (schema: StepSchemaInfo) => {
      const newStep: BuilderStep = {
        id: uniqueId(),
        step: schema.name,
        params: buildDefaultParams(schema),
      };
      setBuilderSteps((prev) => [...prev, newStep]);
      setSelectedId(newStep.id);
    },
    []
  );

  /** Deletes a step from the canvas, clearing the selection if it was the one
   * being edited (otherwise the right panel would reference a dead id). */
  const removeStep = useCallback(
    (id: string) => {
      setBuilderSteps((prev) => prev.filter((s) => s.id !== id));
      if (selectedId === id) setSelectedId(null);
    },
    [selectedId]
  );

  /** Replaces one step's params wholesale (the editor always hands back a full
   * object). Identity-stable so `ParamEditor` does not thrash. */
  const updateStepParams = useCallback(
    (id: string, params: Record<string, unknown>) => {
      setBuilderSteps((prev) =>
        prev.map((s) => (s.id === id ? { ...s, params } : s))
      );
    },
    []
  );

  // Drag-and-drop
  //
  // AI Note: the 5px `activationConstraint` is what makes a card both
  // clickable and draggable — without it, dnd-kit claims the pointer on
  // mousedown and the click-to-select handler never fires. Lowering it makes
  // selection unreliable; raising it makes dragging feel sticky.
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    })
  );

  /** dnd-kit: records which card is being dragged so the overlay can render. */
  function handleDragStart(event: DragStartEvent) {
    setActiveId(String(event.active.id));
  }

  /**
   * dnd-kit: commits a reorder.
   *
   * AI Note: step ORDER is the pipeline's execution order and later steps can
   * consume earlier steps' outputs — reordering can therefore break a job that
   * was previously valid. The server re-validates the output-key chain at
   * submit time; nothing here warns the user.
   *
   * The `oldIndex/newIndex === -1` guard covers a card removed mid-drag; it
   * returns the previous state untouched rather than corrupting the list.
   */
  function handleDragEnd(event: DragEndEvent) {
    setActiveId(null);
    const { active, over } = event;
    if (over && active.id !== over.id) {
      setBuilderSteps((prev) => {
        const oldIndex = prev.findIndex((s) => s.id === active.id);
        const newIndex = prev.findIndex((s) => s.id === over.id);
        if (oldIndex === -1 || newIndex === -1) return prev;
        return arrayMove(prev, oldIndex, newIndex);
      });
    }
  }

  // Canvas drop zone (for items dragged from palette via native drag)
  /**
   * Native-drag drop target for palette items (see the note on
   * {@link PaletteItem}).
   *
   * AI Note: the dropped step is always APPENDED to the end — the drop
   * position within the canvas is ignored. Users expecting "drop between two
   * cards to insert there" will be surprised; reordering afterwards is the
   * intended workflow.
   */
  function handleCanvasDrop(e: React.DragEvent) {
    e.preventDefault();
    const stepName = e.dataTransfer.getData("application/nexus-step");
    if (!stepName) return;
    const schema = schemaMap.get(stepName);
    if (schema) addStep(schema);
  }

  /** Accepts the drag only when it carries our private MIME type, so dragging
   * arbitrary files or text over the canvas does nothing. `preventDefault` is
   * required for a drop event to fire at all. */
  function handleCanvasDragOver(e: React.DragEvent) {
    if (e.dataTransfer.types.includes("application/nexus-step")) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
    }
  }

  // Submit job
  /**
   * Validates locally, serialises the canvas into the API payload, POSTs
   * /api/jobs and navigates to the created job.
   *
   * Payload shape: `{ name, steps: [{ step, params }], target_pool_id?,
   * target_node_id?, priority }`.
   *
   * AI Note: params are filtered before sending — `""`, null and undefined are
   * dropped so the server sees "field absent" (and applies its own default)
   * rather than "field set to empty". `false` is deliberately NOT filtered
   * here (unlike the card's display summary), because an explicitly-off
   * boolean is a meaningful value.
   *
   * AI Note: the two target fields are mutually exclusive by construction —
   * only the one matching `targetMode` is populated, and an unselected
   * dropdown sends `undefined` (no target at all, letting the scheduler pick).
   * Sending both would be ambiguous to the scheduler.
   *
   * AI Note: this is where server-side chain validation errors surface. The
   * server accumulates each step's OUTPUT_KEYS while walking the list, so a
   * step referencing an output that no earlier step produces is rejected here
   * with a descriptive message — that message goes straight into
   * `submitError`.
   */
  async function handleSubmit() {
    setSubmitError(null);
    if (!jobName.trim()) {
      setSubmitError("Job name is required.");
      return;
    }
    if (builderSteps.length === 0) {
      setSubmitError("Add at least one step.");
      return;
    }

    const steps = builderSteps.map((bs) => ({
      step: bs.step,
      params: Object.fromEntries(
        Object.entries(bs.params).filter(
          ([, v]) => v !== "" && v !== null && v !== undefined
        )
      ),
    }));

    setSubmitting(true);
    try {
      const job = await api.submitJob({
        name: jobName.trim(),
        steps,
        target_pool_id: targetMode === "pool" && targetPoolId ? targetPoolId : undefined,
        target_node_id: targetMode === "node" && targetNodeId ? targetNodeId : undefined,
        priority: PRIORITY_VALUES[priority],
      });
      navigate(`/jobs/${job.id}`);
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : "Submission failed");
    } finally {
      setSubmitting(false);
    }
  }

  // Active drag overlay step
  const activeDragStep = builderSteps.find((s) => s.id === activeId);
  const activeDragIndex = activeDragStep
    ? builderSteps.indexOf(activeDragStep)
    : -1;

  // ---------- Render ----------

  return (
    <div className="flex h-full">
      {/* ── Left Panel: Step Palette ─────────────────────────────────── */}
      <div className="w-72 border-r border-border bg-secondary flex flex-col overflow-hidden">
        <div className="p-4 border-b border-border">
          <h2 className="text-sm font-semibold text-foreground flex items-center gap-1.5 mb-3">
            <Layers className="h-4 w-4" />
            Step Palette
          </h2>
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
            <input
              type="text"
              placeholder="Filter steps..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className={cn(
                "w-full pl-8 pr-3 py-1.5 text-sm rounded-md border border-border",
                "focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 outline-none"
              )}
            />
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-3 space-y-4">
          {stepsStore.isLoading && (
            <p className="text-sm text-muted-foreground text-center py-8">
              Loading steps...
            </p>
          )}
          {!stepsStore.isLoading && filteredSteps.length === 0 && (
            <p className="text-sm text-muted-foreground text-center py-8">
              No steps match your search.
            </p>
          )}
          {Object.entries(grouped).map(([category, steps]) => (
            <div key={category}>
              <h3 className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">
                {category}
              </h3>
              <div className="space-y-2">
                {steps.map((s) => (
                  <PaletteItem key={s.name} schema={s} onAdd={addStep} />
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* ── Center Panel: Step Canvas ────────────────────────────────── */}
      <div
        className="flex-1 flex flex-col overflow-hidden bg-secondary"
        onDrop={handleCanvasDrop}
        onDragOver={handleCanvasDragOver}
      >
        <div className="px-6 py-4 border-b border-border bg-card">
          <h1 className="text-lg font-semibold text-foreground">Job Builder</h1>
          <p className="text-sm text-muted-foreground mt-0.5">
            Drag steps from the palette or click to add. Reorder by dragging.
          </p>
        </div>

        <div className="flex-1 overflow-y-auto p-6">
          <DndContext
            sensors={sensors}
            collisionDetection={closestCenter}
            onDragStart={handleDragStart}
            onDragEnd={handleDragEnd}
          >
            <SortableContext
              items={builderSteps.map((s) => s.id)}
              strategy={verticalListSortingStrategy}
            >
              {builderSteps.length === 0 ? (
                <div
                  className={cn(
                    "flex flex-col items-center justify-center",
                    "h-64 rounded-lg border-2 border-dashed border-border",
                    "text-muted-foreground"
                  )}
                >
                  <Package className="h-10 w-10 mb-3 text-muted-foreground" />
                  <p className="text-sm font-medium">
                    Drag steps here to build your job
                  </p>
                  <p className="text-xs mt-1">
                    or click a step in the palette to add it
                  </p>
                </div>
              ) : (
                <div className="space-y-2 max-w-2xl mx-auto">
                  {builderSteps.map((bstep, i) => (
                    <SortableStepCard
                      key={bstep.id}
                      bstep={bstep}
                      index={i}
                      schema={schemaMap.get(bstep.step)}
                      isSelected={bstep.id === selectedId}
                      onSelect={() => setSelectedId(bstep.id)}
                      onRemove={() => removeStep(bstep.id)}
                    />
                  ))}
                </div>
              )}
            </SortableContext>

            <DragOverlay>
              {activeDragStep && (
                <DragOverlayCard
                  bstep={activeDragStep}
                  index={activeDragIndex}
                />
              )}
            </DragOverlay>
          </DndContext>
        </div>
      </div>

      {/* ── Right Panel: Configuration ───────────────────────────────── */}
      <div className="w-80 border-l border-border bg-card flex flex-col overflow-hidden">
        {/* Step param editor */}
        <div className="flex-1 overflow-y-auto">
          {selectedStep && selectedSchema ? (
            <div className="p-4">
              <h2 className="text-sm font-semibold text-foreground mb-1">
                Configure: {selectedStep.step}
              </h2>
              <p className="text-xs text-muted-foreground mb-4">
                {selectedSchema.description}
              </p>
              <ParamEditor
                schema={selectedSchema}
                params={selectedStep.params}
                onChange={(p) => updateStepParams(selectedStep.id, p)}
              />
            </div>
          ) : (
            <div className="flex flex-col items-center justify-center h-48 text-muted-foreground px-4">
              <p className="text-sm text-center">
                {builderSteps.length === 0
                  ? "Add steps to get started"
                  : "Select a step in the canvas to configure its parameters"}
              </p>
            </div>
          )}
        </div>

        {/* Job submission section */}
        <div className="border-t border-border p-4 space-y-4 bg-secondary">
          {/* Job name */}
          <div>
            <label className="block text-sm font-medium text-foreground mb-1">
              Job Name *
            </label>
            <input
              type="text"
              value={jobName}
              onChange={(e) => setJobName(e.target.value)}
              placeholder="e.g. nightly-build-check"
              className={cn(
                "w-full rounded-md border border-border px-3 py-2 text-sm",
                "focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 outline-none"
              )}
            />
          </div>

          {/* Target mode selector */}
          <div>
            <label className="block text-sm font-medium text-foreground mb-1">
              Target
            </label>
            <div className="flex gap-2 mb-2">
              <button
                type="button"
                onClick={() => setTargetMode("pool")}
                className={cn(
                  "flex-1 text-xs py-1.5 rounded-md border font-medium transition-colors",
                  targetMode === "pool"
                    ? "bg-indigo-50 border-indigo-300 text-indigo-700"
                    : "bg-card border-border text-muted-foreground hover:bg-muted"
                )}
              >
                Pool
              </button>
              <button
                type="button"
                onClick={() => setTargetMode("node")}
                className={cn(
                  "flex-1 text-xs py-1.5 rounded-md border font-medium transition-colors",
                  targetMode === "node"
                    ? "bg-indigo-50 border-indigo-300 text-indigo-700"
                    : "bg-card border-border text-muted-foreground hover:bg-muted"
                )}
              >
                Node
              </button>
            </div>

            {targetMode === "pool" ? (
              <select
                value={targetPoolId}
                onChange={(e) => setTargetPoolId(e.target.value)}
                className={cn(
                  "w-full rounded-md border border-border px-3 py-2 text-sm",
                  "focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 outline-none"
                )}
              >
                <option value="">-- Select Pool --</option>
                {poolsStore.pools.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name} ({p.node_count} nodes)
                  </option>
                ))}
              </select>
            ) : (
              <select
                value={targetNodeId}
                onChange={(e) => setTargetNodeId(e.target.value)}
                className={cn(
                  "w-full rounded-md border border-border px-3 py-2 text-sm",
                  "focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 outline-none"
                )}
              >
                <option value="">-- Select Node --</option>
                {nodesStore.nodes
                  // AI Note: only `online` nodes are offered — `busy` nodes
                  // are excluded even though they are perfectly valid targets
                  // (the job would simply queue behind the current one). This
                  // is stricter than the scheduler and can make a healthy
                  // cluster look like it has no available targets.
                  .filter((n) => n.status === "online")
                  .map((n) => (
                    <option key={n.id} value={n.id}>
                      {n.display_name ?? n.hostname} ({n.os_type} / {n.arch})
                    </option>
                  ))}
              </select>
            )}
          </div>

          {/* Priority */}
          <div>
            <label className="block text-sm font-medium text-foreground mb-1">
              Priority
            </label>
            <select
              value={priority}
              onChange={(e) => setPriority(e.target.value as Priority)}
              className={cn(
                "w-full rounded-md border border-border px-3 py-2 text-sm",
                "focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 outline-none"
              )}
            >
              <option value="high">High</option>
              <option value="normal">Normal</option>
              <option value="low">Low</option>
            </select>
          </div>

          {/* Error message */}
          {submitError && (
            <div className="flex items-start gap-2 p-2 rounded-md bg-red-50 border border-red-200 text-red-700 text-xs">
              <AlertCircle className="h-3.5 w-3.5 flex-shrink-0 mt-0.5" />
              <span>{submitError}</span>
            </div>
          )}

          {/* Submit */}
          <button
            type="button"
            onClick={handleSubmit}
            disabled={submitting}
            className={cn(
              "w-full flex items-center justify-center gap-2 py-2.5 px-4 rounded-lg",
              "text-sm font-semibold text-white transition-colors",
              submitting
                ? "bg-indigo-400 cursor-not-allowed"
                : "bg-indigo-600 hover:bg-indigo-700 active:bg-indigo-800"
            )}
          >
            <Play className="h-4 w-4" />
            {submitting ? "Submitting..." : "Submit Job"}
          </button>
        </div>
      </div>
    </div>
  );
}
