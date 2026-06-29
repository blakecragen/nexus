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

interface BuilderStep {
  id: string;
  step: string;
  params: Record<string, unknown>;
}

type TargetMode = "pool" | "node";
type Priority = "high" | "normal" | "low";

const PRIORITY_VALUES: Record<Priority, number> = {
  high: 10,
  normal: 5,
  low: 1,
};

// ---------------------------------------------------------------------------
// Category helpers
// ---------------------------------------------------------------------------

const STEP_CATEGORIES: Record<string, string> = {
  shell: "Shell",
  flow: "Flow Control",
  git: "Git",
  gem5: "gem5",
  package: "Package",
  system: "System",
};

function categorize(stepName: string): string {
  const prefix = stepName.split("_")[0]?.toLowerCase() ?? "";
  return STEP_CATEGORIES[prefix] ?? "Other";
}

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

let _idCounter = 0;
function uniqueId(): string {
  return `step-${Date.now()}-${++_idCounter}`;
}

// ---------------------------------------------------------------------------
// OS badge color
// ---------------------------------------------------------------------------

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

function ParamEditor({
  schema,
  params,
  onChange,
}: {
  schema: StepSchemaInfo;
  params: Record<string, unknown>;
  onChange: (params: Record<string, unknown>) => void;
}) {
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

export default function JobBuilder() {
  const navigate = useNavigate();

  // Store data
  const stepsStore = useStepsStore();
  const poolsStore = usePoolsStore();
  const nodesStore = useNodesStore();

  // Fetch on mount
  useEffect(() => {
    stepsStore.fetch();
    poolsStore.fetch();
    nodesStore.fetch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Palette search
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
  const schemaMap = useMemo(() => {
    const m = new Map<string, StepSchemaInfo>();
    for (const s of stepsStore.steps) m.set(s.name, s);
    return m;
  }, [stepsStore.steps]);

  // Builder state
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
  const selectedStep = builderSteps.find((s) => s.id === selectedId);
  const selectedSchema = selectedStep
    ? schemaMap.get(selectedStep.step)
    : undefined;

  // -- Handlers --

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

  const removeStep = useCallback(
    (id: string) => {
      setBuilderSteps((prev) => prev.filter((s) => s.id !== id));
      if (selectedId === id) setSelectedId(null);
    },
    [selectedId]
  );

  const updateStepParams = useCallback(
    (id: string, params: Record<string, unknown>) => {
      setBuilderSteps((prev) =>
        prev.map((s) => (s.id === id ? { ...s, params } : s))
      );
    },
    []
  );

  // Drag-and-drop
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    })
  );

  function handleDragStart(event: DragStartEvent) {
    setActiveId(String(event.active.id));
  }

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
  function handleCanvasDrop(e: React.DragEvent) {
    e.preventDefault();
    const stepName = e.dataTransfer.getData("application/nexus-step");
    if (!stepName) return;
    const schema = schemaMap.get(stepName);
    if (schema) addStep(schema);
  }

  function handleCanvasDragOver(e: React.DragEvent) {
    if (e.dataTransfer.types.includes("application/nexus-step")) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
    }
  }

  // Submit job
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
