import {
  DatabaseZap,
  FileText,
  Minus,
  MousePointer2,
  Plus,
  StickyNote,
  Trash2,
  Upload,
  Webhook,
  Zap,
  MessageSquare,
  ListRestart
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { Dispatch, ReactNode, SetStateAction } from "react";
import { actionCatalog, actionNodeSubtitle, actionNodeTitle, defaultNodeData, NODE_HEIGHT, NODE_WIDTH } from "./workflows";
import type { ActionType, DiagramShape, NodeData, Point, Tool } from "./types";

export type PaletteKind = "trigger" | ActionType | "note";

interface WorkflowBoardProps {
  shapes: DiagramShape[];
  setShapes: Dispatch<SetStateAction<DiagramShape[]>>;
  selectedId: string | null;
  setSelectedId: Dispatch<SetStateAction<string | null>>;
  sessionControls?: (actions: {
    canRedo: boolean;
    canUndo: boolean;
    clearCanvas: () => void;
    redo: () => void;
    undo: () => void;
  }) => ReactNode;
}

type ConnectionHandle = { id: string; x: number; y: number };

const nodeColor = "var(--diagram-component)";
const triggerColor = "var(--diagram-trigger)";
const noteColor = "var(--diagram-note)";
const handleColor = "var(--diagram-handle)";
const shapeLabelSize = 16;
const minZoom = 0.5;
const maxZoom = 2;

export function actionIcon(type: ActionType) {
  if (type === "webhook") return Webhook;
  if (type === "slack") return MessageSquare;
  if (type === "dataops") return DatabaseZap;
  if (type === "dropbox_upload") return Upload;
  return FileText;
}

const paletteEntries: Array<{ kind: PaletteKind; label: string; icon: typeof Zap }> = [
  { kind: "trigger", label: "Trigger", icon: Zap },
  ...actionCatalog.map((entry) => ({ kind: entry.type as PaletteKind, label: entry.label, icon: actionIcon(entry.type) })),
  { kind: "note", label: "Note", icon: StickyNote }
];

function paletteIcon(kind: PaletteKind) {
  return paletteEntries.find((entry) => entry.kind === kind)?.icon ?? Zap;
}

function paletteLabel(kind: PaletteKind) {
  return paletteEntries.find((entry) => entry.kind === kind)?.label ?? "Node";
}

function shapeColor(shape: DiagramShape) {
  if (shape.type === "note") return noteColor;
  if (shape.data?.nodeKind === "trigger") return triggerColor;
  return nodeColor;
}

function centerOf(shape: DiagramShape): Point {
  return { x: shape.x + shape.width / 2, y: shape.y + shape.height / 2 };
}

function shapeContains(shape: DiagramShape, point: Point) {
  return point.x >= shape.x - 10
    && point.x <= shape.x + shape.width + 10
    && point.y >= shape.y - 10
    && point.y <= shape.y + shape.height + 10;
}

function findNodeAt(shapes: DiagramShape[], point: Point) {
  return [...shapes].reverse().find((shape) => shape.type !== "arrow" && shapeContains(shape, point));
}

function isNodeShape(shape: DiagramShape | null | undefined): shape is DiagramShape {
  return Boolean(shape && shape.type === "node");
}

function distanceToSegment(point: Point, start: Point, end: Point) {
  const dx = end.x - start.x;
  const dy = end.y - start.y;
  const lengthSquared = dx * dx + dy * dy;
  if (lengthSquared === 0) return Math.hypot(point.x - start.x, point.y - start.y);
  const t = Math.max(0, Math.min(1, ((point.x - start.x) * dx + (point.y - start.y) * dy) / lengthSquared));
  const projection = { x: start.x + t * dx, y: start.y + t * dy };
  return Math.hypot(point.x - projection.x, point.y - projection.y);
}

function displayLabel(label: string) {
  return label.length > 26 ? `${label.slice(0, 23)}...` : label;
}

function connectionHandles(shape: DiagramShape): ConnectionHandle[] {
  return [
    { id: "top", x: shape.x + shape.width / 2, y: shape.y },
    { id: "right", x: shape.x + shape.width, y: shape.y + shape.height / 2 },
    { id: "bottom", x: shape.x + shape.width / 2, y: shape.y + shape.height },
    { id: "left", x: shape.x, y: shape.y + shape.height / 2 }
  ];
}

function nearestConnectionHandle(shape: DiagramShape, point: Point) {
  return connectionHandles(shape).reduce((nearest, handle) => (
    Math.hypot(point.x - handle.x, point.y - handle.y) < Math.hypot(point.x - nearest.x, point.y - nearest.y)
      ? handle
      : nearest
  ));
}

function connectionHandleById(shape: DiagramShape, handleId: string | undefined) {
  return connectionHandles(shape).find((handle) => handle.id === handleId);
}

function inferConnectionHandles(source: DiagramShape, target: DiagramShape) {
  const sourceHandles = connectionHandles(source);
  const targetHandles = connectionHandles(target);
  let best = { source: sourceHandles[0], target: targetHandles[0], distance: Number.POSITIVE_INFINITY };
  for (const sourceHandle of sourceHandles) {
    for (const targetHandle of targetHandles) {
      const distance = Math.hypot(sourceHandle.x - targetHandle.x, sourceHandle.y - targetHandle.y);
      if (distance < best.distance) best = { source: sourceHandle, target: targetHandle, distance };
    }
  }
  return best;
}

function connectorEndpoints(shape: DiagramShape, shapes: DiagramShape[]) {
  const source = shapes.find((candidate) => candidate.id === shape.sourceId);
  const target = shapes.find((candidate) => candidate.id === shape.targetId);
  if (source && target) {
    const inferred = inferConnectionHandles(source, target);
    const sourceHandle = connectionHandleById(source, shape.sourceHandleId) ?? inferred.source;
    const targetHandle = connectionHandleById(target, shape.targetHandleId) ?? inferred.target;
    return { start: sourceHandle, end: targetHandle };
  }
  return {
    start: { x: shape.x, y: shape.y },
    end: { x: shape.x + shape.width, y: shape.y + shape.height }
  };
}

function refreshConnectedArrow(shape: DiagramShape, shapes: DiagramShape[]) {
  if (shape.type !== "arrow") return shape;
  const source = shapes.find((candidate) => candidate.id === shape.sourceId);
  const target = shapes.find((candidate) => candidate.id === shape.targetId);
  if (!source || !target) return shape;
  const handles = inferConnectionHandles(source, target);
  return {
    ...shape,
    sourceHandleId: handles.source.id,
    targetHandleId: handles.target.id,
    x: handles.source.x,
    y: handles.source.y,
    width: handles.target.x - handles.source.x,
    height: handles.target.y - handles.source.y
  };
}

function refreshArrowsForMovedShape(shapes: DiagramShape[], movedShapeId: string) {
  return shapes.map((shape) => (
    shape.sourceId === movedShapeId || shape.targetId === movedShapeId
      ? refreshConnectedArrow(shape, shapes)
      : shape
  ));
}

function findConnectorAt(shapes: DiagramShape[], point: Point) {
  return [...shapes].reverse().find((shape) => {
    if (shape.type !== "arrow") return false;
    const endpoints = connectorEndpoints(shape, shapes);
    return distanceToSegment(point, endpoints.start, endpoints.end) <= 12;
  });
}

function nodeDataForKind(kind: PaletteKind): NodeData {
  if (kind === "note") return { nodeKind: "note" };
  if (kind === "trigger") return defaultNodeData("trigger");
  return defaultNodeData("action");
}

function nodeTitle(shape: DiagramShape): string {
  if (shape.type === "note") return shape.label ?? "Note";
  if (shape.data?.nodeKind === "trigger") return `${shape.data.connector} · ${shape.data.event}`;
  return actionNodeTitle(shape.data ?? { nodeKind: "action" });
}

function nodeSubtitle(shape: DiagramShape): string {
  if (shape.type === "note" || shape.data?.nodeKind === "trigger") return "";
  return actionNodeSubtitle(shape.data ?? { nodeKind: "action" });
}

export function WorkflowBoard({
  shapes,
  setShapes,
  selectedId,
  setSelectedId,
  sessionControls
}: WorkflowBoardProps) {
  const [tool, setTool] = useState<Tool>("select");
  const [paletteKind, setPaletteKind] = useState<PaletteKind>("webhook");
  const [dragStart, setDragStart] = useState<Point | null>(null);
  const [panStart, setPanStart] = useState<{ clientX: number; clientY: number; origin: Point } | null>(null);
  const [connectorDrag, setConnectorDrag] = useState<{ sourceId: string; sourceHandleId: string; start: Point; current: Point } | null>(null);
  const [reattachDrag, setReattachDrag] = useState<{ arrowId: string; endpoint: "source" | "target"; fixed: Point; current: Point } | null>(null);
  const [draggingKind, setDraggingKind] = useState<PaletteKind | null>(null);
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number; point: Point; shapeId?: string } | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingLabel, setEditingLabel] = useState("");
  const [canvasViewBox, setCanvasViewBox] = useState({ width: 1200, height: 760 });
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState<Point>({ x: 0, y: 0 });
  const [undoStack, setUndoStack] = useState<DiagramShape[][]>([]);
  const [redoStack, setRedoStack] = useState<DiagramShape[][]>([]);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const editorRef = useRef<HTMLInputElement | null>(null);
  const dragSnapshotRef = useRef<DiagramShape[] | null>(null);
  const didDragRef = useRef(false);
  const skipNextEditCommitRef = useRef(false);

  const selectedShape = shapes.find((shape) => shape.id === selectedId) ?? null;
  const editingShape = shapes.find((shape) => shape.id === editingId) ?? null;
  const canvasClass = useMemo(() => (
    [
      "drawing-surface",
      tool === "select" ? "selecting" : "",
      panStart ? "panning" : "",
      draggingKind ? "component-dropping" : ""
    ].filter(Boolean).join(" ")
  ), [draggingKind, panStart, tool]);
  const zoomedViewBox = useMemo(() => {
    const width = canvasViewBox.width / zoom;
    const height = canvasViewBox.height / zoom;
    return {
      x: (canvasViewBox.width - width) / 2 + pan.x,
      y: (canvasViewBox.height - height) / 2 + pan.y,
      width,
      height
    };
  }, [canvasViewBox, pan, zoom]);

  function toCanvasPoint(event: { clientX: number; clientY: number }) {
    const svg = svgRef.current;
    if (!svg) return { x: 0, y: 0 };
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    const transformed = point.matrixTransform(svg.getScreenCTM()?.inverse());
    return { x: transformed.x, y: transformed.y };
  }

  function toViewportPoint(point: Point) {
    const svg = svgRef.current;
    const matrix = svg?.getScreenCTM();
    if (!svg || !matrix) return null;
    const svgPoint = svg.createSVGPoint();
    svgPoint.x = point.x;
    svgPoint.y = point.y;
    const transformed = svgPoint.matrixTransform(matrix);
    return { x: transformed.x, y: transformed.y };
  }

  function toViewportFontSize() {
    const matrix = svgRef.current?.getScreenCTM();
    if (!matrix) return shapeLabelSize;
    return Math.max(11, Math.round(shapeLabelSize * Math.abs(matrix.d) * 10) / 10);
  }

  function rememberHistory(previousShapes: DiagramShape[]) {
    setUndoStack((currentStack) => [...currentStack.slice(-49), previousShapes]);
    setRedoStack([]);
  }

  function commitShapes(updater: (currentShapes: DiagramShape[]) => DiagramShape[]) {
    const nextShapes = updater(shapes);
    if (nextShapes === shapes) return;
    rememberHistory(shapes);
    setShapes(nextShapes);
  }

  function addShape(point: Point, kind: PaletteKind) {
    if (kind === "note") {
      const next: DiagramShape = {
        id: crypto.randomUUID(),
        type: "note",
        x: point.x - 110,
        y: point.y - 44,
        width: 220,
        height: 88,
        label: "Note"
      };
      commitShapes((currentShapes) => [...currentShapes, next]);
      setSelectedId(next.id);
    } else {
      const next: DiagramShape = {
        id: crypto.randomUUID(),
        type: "node",
        x: point.x - NODE_WIDTH / 2,
        y: point.y - NODE_HEIGHT / 2,
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
        label: paletteLabel(kind),
        data: nodeDataForKind(kind)
      };
      commitShapes((currentShapes) => [...currentShapes, next]);
      setSelectedId(next.id);
    }
    setTool("select");
  }

  function addConnector(sourceId: string, sourceHandleId: string, target: DiagramShape, targetPoint: Point) {
    const source = shapes.find((shape) => shape.id === sourceId);
    if (!isNodeShape(source) || !isNodeShape(target)) return;
    if (sourceId === target.id || target.data?.nodeKind === "trigger") return;
    const sourceHandle = connectionHandleById(source, sourceHandleId) ?? nearestConnectionHandle(source, centerOf(target));
    const targetHandle = nearestConnectionHandle(target, targetPoint);
    const next: DiagramShape = {
      id: crypto.randomUUID(),
      type: "arrow",
      x: sourceHandle.x,
      y: sourceHandle.y,
      width: targetHandle.x - sourceHandle.x,
      height: targetHandle.y - sourceHandle.y,
      sourceId: source.id,
      targetId: target.id,
      sourceHandleId: sourceHandle.id,
      targetHandleId: targetHandle.id
    };
    commitShapes((currentShapes) => [...currentShapes, next]);
    setSelectedId(next.id);
    setConnectorDrag(null);
    setTool("select");
  }

  function onPointerDown(event: React.PointerEvent<SVGSVGElement>) {
    const point = toCanvasPoint(event);
    const hit = findNodeAt(shapes, point);
    const connectorHit = hit ? undefined : findConnectorAt(shapes, point);

    if (tool !== "select") {
      event.stopPropagation();
      addShape(point, paletteKind);
      return;
    }

    const draggableHit = hit && hit.type !== "arrow";
    setSelectedId(hit?.id ?? connectorHit?.id ?? null);
    setDragStart(draggableHit ? point : null);
    if (draggableHit) {
      dragSnapshotRef.current = shapes;
      didDragRef.current = false;
      event.currentTarget.setPointerCapture(event.pointerId);
    } else if (!connectorHit) {
      setPanStart({ clientX: event.clientX, clientY: event.clientY, origin: pan });
      didDragRef.current = false;
      event.currentTarget.setPointerCapture(event.pointerId);
    }
  }

  function onPointerMove(event: React.PointerEvent<SVGSVGElement>) {
    if (panStart) {
      const svg = svgRef.current;
      if (!svg) return;
      const rect = svg.getBoundingClientRect();
      const dx = ((event.clientX - panStart.clientX) * zoomedViewBox.width) / rect.width;
      const dy = ((event.clientY - panStart.clientY) * zoomedViewBox.height) / rect.height;
      if (dx !== 0 || dy !== 0) didDragRef.current = true;
      setPan({ x: panStart.origin.x - dx, y: panStart.origin.y - dy });
      return;
    }
    if (reattachDrag) {
      setReattachDrag({ ...reattachDrag, current: toCanvasPoint(event) });
      return;
    }
    if (connectorDrag) {
      setConnectorDrag({ ...connectorDrag, current: toCanvasPoint(event) });
      return;
    }
    if (tool !== "select" || !selectedId || !dragStart) return;
    const point = toCanvasPoint(event);
    const dx = point.x - dragStart.x;
    const dy = point.y - dragStart.y;
    if (dx !== 0 || dy !== 0) didDragRef.current = true;
    setShapes((currentShapes) => {
      const movedShapes = currentShapes.map((shape) => {
        if (shape.id !== selectedId) return shape;
        return { ...shape, x: shape.x + dx, y: shape.y + dy };
      });
      return refreshArrowsForMovedShape(movedShapes, selectedId);
    });
    setDragStart(point);
  }

  function onPointerUp(event: React.PointerEvent<SVGSVGElement>) {
    if (reattachDrag) {
      const point = toCanvasPoint(event);
      const target = findNodeAt(shapes, point);
      if (isNodeShape(target) && target.data?.nodeKind !== "trigger") {
        commitShapes((currentShapes) => currentShapes.map((shape) => {
          if (shape.id !== reattachDrag.arrowId || shape.type !== "arrow") return shape;
          if (reattachDrag.endpoint === "source" && target.id === shape.targetId) return shape;
          if (reattachDrag.endpoint === "target" && target.id === shape.sourceId) return shape;
          const targetHandle = nearestConnectionHandle(target, point);
          const nextShape = reattachDrag.endpoint === "source"
            ? { ...shape, sourceId: target.id, sourceHandleId: targetHandle.id }
            : { ...shape, targetId: target.id, targetHandleId: targetHandle.id };
          const endpoints = connectorEndpoints(nextShape, currentShapes);
          return {
            ...nextShape,
            x: endpoints.start.x,
            y: endpoints.start.y,
            width: endpoints.end.x - endpoints.start.x,
            height: endpoints.end.y - endpoints.start.y
          };
        }));
      }
      setReattachDrag(null);
      return;
    }
    if (connectorDrag) {
      const point = toCanvasPoint(event);
      const target = findNodeAt(shapes, point);
      if (isNodeShape(target)) addConnector(connectorDrag.sourceId, connectorDrag.sourceHandleId, target, point);
      setConnectorDrag(null);
      return;
    }
    if (didDragRef.current && dragSnapshotRef.current) {
      rememberHistory(dragSnapshotRef.current);
    }
    setPanStart(null);
    dragSnapshotRef.current = null;
    didDragRef.current = false;
    setDragStart(null);
  }

  function selectTool(nextTool: Tool) {
    setTool(nextTool);
    setConnectorDrag(null);
    setPanStart(null);
    setContextMenu(null);
  }

  function startChipDrag(event: React.DragEvent<HTMLButtonElement>, kind: PaletteKind) {
    setPaletteKind(kind);
    setDraggingKind(kind);
    event.dataTransfer.effectAllowed = "copy";
    event.dataTransfer.setData("application/x-dapier-node", kind);
    event.dataTransfer.setData("text/plain", kind);
  }

  function onCanvasDragOver(event: React.DragEvent<SVGSVGElement>) {
    if (!draggingKind) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  }

  function onCanvasDrop(event: React.DragEvent<SVGSVGElement>) {
    const droppedKind = (event.dataTransfer.getData("application/x-dapier-node") || draggingKind) as PaletteKind | "";
    if (!droppedKind) return;
    event.preventDefault();
    addShape(toCanvasPoint(event), droppedKind);
    setDraggingKind(null);
  }

  function changeZoom(delta: number) {
    setZoom((current) => Math.min(maxZoom, Math.max(minZoom, Math.round((current + delta) * 10) / 10)));
  }

  function deleteSelected() {
    if (!selectedId) return;
    commitShapes((currentShapes) => currentShapes.filter((shape) => (
      shape.id !== selectedId && shape.sourceId !== selectedId && shape.targetId !== selectedId
    )));
    setSelectedId(null);
    setContextMenu(null);
  }

  function undo() {
    const previousShapes = undoStack.at(-1);
    if (!previousShapes) return;
    setUndoStack((currentStack) => currentStack.slice(0, -1));
    setRedoStack((currentStack) => [...currentStack.slice(-49), shapes]);
    setShapes(previousShapes);
    if (selectedId && !previousShapes.some((shape) => shape.id === selectedId)) setSelectedId(null);
    setContextMenu(null);
  }

  function redo() {
    const nextShapes = redoStack.at(-1);
    if (!nextShapes) return;
    setRedoStack((currentStack) => currentStack.slice(0, -1));
    setUndoStack((currentStack) => [...currentStack.slice(-49), shapes]);
    setShapes(nextShapes);
    if (selectedId && !nextShapes.some((shape) => shape.id === selectedId)) setSelectedId(null);
    setContextMenu(null);
  }

  function clearShapes() {
    if (shapes.length && !window.confirm("Clear the canvas? This removes all nodes and connectors.")) return;
    commitShapes(() => []);
    setSelectedId(null);
    setConnectorDrag(null);
    setReattachDrag(null);
    setContextMenu(null);
    setTool("select");
  }

  function openEditor(shape: DiagramShape) {
    setSelectedId(shape.id);
    setContextMenu(null);
    skipNextEditCommitRef.current = false;
    setEditingId(shape.id);
    setEditingLabel(shape.label ?? "");
  }

  function commitEditing() {
    if (skipNextEditCommitRef.current) {
      skipNextEditCommitRef.current = false;
      return;
    }
    if (!editingId) return;
    const nextLabel = editingLabel.trim();
    commitShapes((currentShapes) => currentShapes.map((shape) => (
      shape.id === editingId ? { ...shape, label: nextLabel || undefined } : shape
    )));
    skipNextEditCommitRef.current = true;
    setEditingId(null);
    setEditingLabel("");
  }

  function cancelEditing() {
    skipNextEditCommitRef.current = true;
    setEditingId(null);
    setEditingLabel("");
  }

  function onContextMenu(event: React.MouseEvent<SVGSVGElement>) {
    event.preventDefault();
    const point = toCanvasPoint(event);
    const hit = findNodeAt(shapes, point);
    const connectorHit = hit ? undefined : findConnectorAt(shapes, point);
    setSelectedId(hit?.id ?? connectorHit?.id ?? null);
    setContextMenu({ x: event.clientX, y: event.clientY, point, shapeId: hit?.id ?? connectorHit?.id });
  }

  function onCanvasDoubleClick(event: React.MouseEvent<SVGSVGElement>) {
    const point = toCanvasPoint(event);
    if (findNodeAt(shapes, point) || findConnectorAt(shapes, point) || tool !== "select") return;
    event.preventDefault();
    addShape(point, paletteKind);
  }

  function startConnectorDrag(event: React.PointerEvent<SVGCircleElement>, sourceId: string, start: ConnectionHandle) {
    event.stopPropagation();
    const source = shapes.find((shape) => shape.id === sourceId);
    if (!isNodeShape(source)) return;
    setSelectedId(sourceId);
    setConnectorDrag({ sourceId, sourceHandleId: start.id, start, current: start });
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function startReattachDrag(event: React.PointerEvent<SVGCircleElement>, arrow: DiagramShape, endpoint: "source" | "target") {
    event.stopPropagation();
    const endpoints = connectorEndpoints(arrow, shapes);
    setSelectedId(arrow.id);
    setReattachDrag({
      arrowId: arrow.id,
      endpoint,
      fixed: endpoint === "source" ? endpoints.end : endpoints.start,
      current: endpoint === "source" ? endpoints.start : endpoints.end
    });
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function changeActionType(shapeId: string, actionType: ActionType) {
    commitShapes((currentShapes) => currentShapes.map((shape) => {
      if (shape.id !== shapeId || shape.type !== "node" || shape.data?.nodeKind !== "action") return shape;
      return {
        ...shape,
        label: actionCatalog.find((entry) => entry.type === actionType)?.label ?? shape.label,
        data: { ...defaultNodeData("action"), actionType }
      };
    }));
    setContextMenu(null);
  }

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      const target = event.target;
      if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) {
        return;
      }
      if ((event.key === "Delete" || event.key === "Backspace") && selectedId) {
        event.preventDefault();
        deleteSelected();
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "z" && !event.shiftKey) {
        event.preventDefault();
        undo();
      }
      if ((event.metaKey || event.ctrlKey) && (event.key.toLowerCase() === "y" || (event.shiftKey && event.key.toLowerCase() === "z"))) {
        event.preventDefault();
        redo();
      }
      if (event.key === "Escape") {
        setContextMenu(null);
        cancelEditing();
      }
    }

    function onPointerDown() {
      setContextMenu(null);
    }

    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("pointerdown", onPointerDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("pointerdown", onPointerDown);
    };
  });

  useEffect(() => {
    if (!editingId) return;
    editorRef.current?.focus();
    editorRef.current?.select();
  }, [editingId]);

  useEffect(() => {
    const currentSvg = svgRef.current;
    if (!currentSvg) return;
    const observedSvg = currentSvg;

    function updateViewBox() {
      const rect = observedSvg.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return;
      const nextHeight = Math.round((1200 * rect.height) / rect.width);
      setCanvasViewBox((current) => (
        current.height === nextHeight ? current : { width: 1200, height: nextHeight }
      ));
    }

    updateViewBox();
    const observer = new ResizeObserver(updateViewBox);
    observer.observe(observedSvg);
    window.addEventListener("resize", updateViewBox);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", updateViewBox);
    };
  }, []);

  const editorPosition = editingShape ? toViewportPoint(centerOf(editingShape)) : null;
  const editorFontSize = toViewportFontSize();
  const editorWidth = Math.min(280, Math.max(60, editingLabel.length * editorFontSize * 0.62 + 18));
  const contextShape = contextMenu?.shapeId ? shapes.find((shape) => shape.id === contextMenu.shapeId) : null;

  return (
    <section className="board-panel" aria-label="Workflow board">
      <div className="board-toolbar">
        <div className="tool-strip" aria-label="Board actions">
          <button className={tool === "select" ? "icon-button active" : "icon-button"} onClick={() => selectTool("select")} title="Pointer" type="button">
            <MousePointer2 size={18} />
          </button>
        </div>
        <div className="component-toolbar" aria-label="Node types">
          {paletteEntries.map(({ kind, label, icon: Icon }) => (
            <button
              key={kind}
              aria-grabbed={draggingKind === kind}
              className={tool === "component" && paletteKind === kind ? "component-chip active" : "component-chip"}
              draggable
              onClick={() => { setPaletteKind(kind); selectTool("component"); }}
              onDragEnd={() => setDraggingKind(null)}
              onDragStart={(event) => startChipDrag(event, kind)}
              title={label}
              type="button"
            >
              <Icon size={15} />
            </button>
          ))}
        </div>
        {sessionControls && (
          <div className="canvas-session-controls">
            {sessionControls({
              canRedo: redoStack.length > 0,
              canUndo: undoStack.length > 0,
              clearCanvas: clearShapes,
              redo,
              undo
            })}
          </div>
        )}
      </div>

      <div className="canvas-zoom-controls" aria-label="Canvas zoom controls">
        <button className="icon-button" onClick={() => changeZoom(-0.1)} disabled={zoom <= minZoom} title="Zoom out" type="button">
          <Minus size={18} />
        </button>
        <button className="zoom-button" onClick={() => { setZoom(1); setPan({ x: 0, y: 0 }); }} title="Reset zoom" type="button">
          {Math.round(zoom * 100)}%
        </button>
        <button className="icon-button" onClick={() => changeZoom(0.1)} disabled={zoom >= maxZoom} title="Zoom in" type="button">
          <Plus size={18} />
        </button>
      </div>

      <svg
        ref={svgRef}
        className={canvasClass}
        viewBox={`${zoomedViewBox.x} ${zoomedViewBox.y} ${zoomedViewBox.width} ${zoomedViewBox.height}`}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onDoubleClick={onCanvasDoubleClick}
        onDragOver={onCanvasDragOver}
        onDrop={onCanvasDrop}
        onContextMenu={onContextMenu}
        preserveAspectRatio="none"
      >
        <defs>
          <pattern id="grid" width="28" height="28" patternUnits="userSpaceOnUse">
            <path d="M 28 0 L 0 0 0 28" fill="none" stroke="var(--diagram-grid)" strokeWidth="1" />
          </pattern>
          <marker id="connector-preview-arrowhead" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto">
            <path d="M 0 0 L 8 3 L 0 6 z" fill={nodeColor} />
          </marker>
        </defs>
        <rect x={zoomedViewBox.x} y={zoomedViewBox.y} width={zoomedViewBox.width} height={zoomedViewBox.height} fill="url(#grid)" />
        {connectorDrag && (
          <line
            x1={connectorDrag.start.x}
            y1={connectorDrag.start.y}
            x2={connectorDrag.current.x}
            y2={connectorDrag.current.y}
            stroke={nodeColor}
            strokeLinecap="round"
            strokeWidth="2"
            markerEnd="url(#connector-preview-arrowhead)"
          />
        )}
        {reattachDrag && (
          <line
            x1={reattachDrag.endpoint === "source" ? reattachDrag.current.x : reattachDrag.fixed.x}
            y1={reattachDrag.endpoint === "source" ? reattachDrag.current.y : reattachDrag.fixed.y}
            x2={reattachDrag.endpoint === "source" ? reattachDrag.fixed.x : reattachDrag.current.x}
            y2={reattachDrag.endpoint === "source" ? reattachDrag.fixed.y : reattachDrag.current.y}
            stroke={nodeColor}
            strokeLinecap="round"
            strokeWidth="2"
            markerEnd="url(#connector-preview-arrowhead)"
          />
        )}
        {shapes.map((shape) => {
          const selected = shape.id === selectedId;
          const strokeWidth = selected ? "3" : "2";
          const color = shapeColor(shape);

          if (shape.type === "node") {
            const Icon = paletteIcon(shape.data?.nodeKind === "trigger" ? "trigger" : shape.data?.actionType ?? "webhook");
            const title = nodeTitle(shape);
            const subtitle = nodeSubtitle(shape);
            return (
              <g key={shape.id} onDoubleClick={(event) => { event.stopPropagation(); openEditor(shape); }}>
                <rect
                  x={shape.x}
                  y={shape.y}
                  width={shape.width}
                  height={shape.height}
                  rx="10"
                  fill="var(--diagram-node-bg)"
                  stroke={color}
                  strokeWidth={strokeWidth}
                />
                <rect x={shape.x + 14} y={shape.y + (shape.height - 36) / 2} width="36" height="36" rx="9" fill="var(--diagram-node-accent)" />
                <Icon
                  size={22}
                  x={shape.x + 21}
                  y={shape.y + (shape.height - 36) / 2 + 7}
                  color={color}
                  strokeWidth={1.8}
                />
                <text x={shape.x + 62} y={shape.y + (subtitle ? 40 : 53)} fill="var(--text-strong)" fontSize={subtitle ? 15 : 13} fontWeight="600">
                  <title>{shape.label}</title>
                  {displayLabel(title)}
                </text>
                {subtitle && (
                  <text x={shape.x + 62} y={shape.y + 62} fill="var(--text-muted)" fontSize="12">
                    {displayLabel(subtitle)}
                  </text>
                )}
              </g>
            );
          }

          if (shape.type === "note") {
            return (
              <g key={shape.id} onDoubleClick={(event) => { event.stopPropagation(); openEditor(shape); }}>
                <rect x={shape.x} y={shape.y} width={shape.width} height={shape.height} rx="6" fill="var(--note-bg)" stroke={noteColor} strokeWidth={strokeWidth} />
                <text x={shape.x + 14} y={shape.y + 30} fill="var(--text-strong)" fontSize={shapeLabelSize} fontWeight="500">
                  {shape.label && <title>{shape.label}</title>}
                  {shape.label ? displayLabel(shape.label) : ""}
                </text>
              </g>
            );
          }

          const endpoints = connectorEndpoints(shape, shapes);
          const midpoint = {
            x: (endpoints.start.x + endpoints.end.x) / 2,
            y: (endpoints.start.y + endpoints.end.y) / 2
          };
          return (
            <g key={shape.id}>
              <defs>
                <marker id={`arrowhead-${shape.id}`} markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto">
                  <path d="M 0 0 L 8 3 L 0 6 z" fill={color} />
                </marker>
              </defs>
              <line className="connector-hitline" x1={endpoints.start.x} y1={endpoints.start.y} x2={endpoints.end.x} y2={endpoints.end.y} stroke="transparent" strokeWidth="18" />
              <line
                x1={endpoints.start.x}
                y1={endpoints.start.y}
                x2={endpoints.end.x}
                y2={endpoints.end.y}
                stroke={color}
                strokeLinecap="round"
                strokeWidth="2"
                markerEnd={`url(#arrowhead-${shape.id})`}
              />
              {selected && (
                <>
                  <circle className="connection-handle" cx={endpoints.start.x} cy={endpoints.start.y} r="8" fill={handleColor} stroke="var(--diagram-node-bg)" strokeWidth="3"
                    onPointerDown={(event) => startReattachDrag(event, shape, "source")} />
                  <circle className="connection-handle" cx={endpoints.end.x} cy={endpoints.end.y} r="8" fill={handleColor} stroke="var(--diagram-node-bg)" strokeWidth="3"
                    onPointerDown={(event) => startReattachDrag(event, shape, "target")} />
                </>
              )}
              {selected && <circle cx={midpoint.x} cy={midpoint.y} r="3" fill={color} />}
            </g>
          );
        })}
        {isNodeShape(selectedShape) && connectionHandles(selectedShape).map((handle) => (
          <circle
            key={`${selectedShape.id}-${handle.id}`}
            className="connection-handle"
            cx={handle.x}
            cy={handle.y}
            r="8"
            fill={handleColor}
            stroke="var(--diagram-node-bg)"
            strokeWidth="3"
            onPointerDown={(event) => startConnectorDrag(event, selectedShape.id, handle)}
          />
        ))}
      </svg>

      {editingShape && editorPosition && (
        <input
          ref={editorRef}
          className="label-editor"
          value={editingLabel}
          onChange={(event) => setEditingLabel(event.target.value)}
          onBlur={commitEditing}
          onKeyDown={(event) => {
            if (event.key === "Enter") commitEditing();
            if (event.key === "Escape") cancelEditing();
          }}
          style={{ fontSize: editorFontSize, left: editorPosition.x, top: editorPosition.y, width: editorWidth }}
          aria-label="Edit node title"
        />
      )}

      {contextMenu && (
        <div className="canvas-context-menu" style={{ left: contextMenu.x, top: contextMenu.y }} onPointerDown={(event) => event.stopPropagation()}>
          <button onClick={() => { addShape(contextMenu.point, "note"); setContextMenu(null); }} type="button">
            <StickyNote size={16} />
            Add note
          </button>
          {contextShape && contextShape.type === "node" && contextShape.data?.nodeKind === "action" && (
            <div className="context-menu-group">
              <div className="context-menu-label">
                <ListRestart size={14} />
                Change action type
              </div>
              {actionCatalog.map(({ type, label }) => {
                const EntryIcon = actionIcon(type);
                return (
                  <button
                    key={type}
                    className={contextShape.data?.actionType === type ? "active" : ""}
                    onClick={() => changeActionType(contextMenu.shapeId!, type)}
                    type="button"
                  >
                    <EntryIcon size={16} />
                    {label}
                  </button>
                );
              })}
            </div>
          )}
          {contextMenu.shapeId && (
            <button className="danger" onClick={deleteSelected} disabled={selectedId !== contextMenu.shapeId} type="button">
              <Trash2 size={16} />
              Delete
            </button>
          )}
        </div>
      )}
    </section>
  );
}
