import {
  ClipboardCopy,
  ClipboardPaste,
  Copy,
  ListPlus,
  ListRestart,
  Maximize,
  Minus,
  Plus,
  StickyNote,
  Trash2,
  Zap
} from "../icons";
import { useEffect, useMemo, useRef, useState } from "react";
import type { Dispatch, ReactNode, SetStateAction } from "react";
import { ConnectionHandle, nodeColor, noteColor, handleColor, shapeLabelSize, minZoom, maxZoom, shapeColor, centerOf, findNodeAt, isNodeShape, displayLabel, connectionHandles, nearestConnectionHandle, connectionHandleById, connectorEndpoints, refreshArrowsForMovedShape, findConnectorAt } from "./geometry";
import { PaletteKind, paletteLabel, nodeDataForKind, nodeIcon, nodeTitle, nodeSubtitle } from "./nodes";
import { PickerMode, StepPicker } from "./StepPicker";
import { actionMeta, defaultFields, defaultNodeData, NODE_HEIGHT, NODE_WIDTH } from "../workflows";
import type { ActionType, DiagramShape, Point } from "../types";

/** An open step picker: what it offers, and what a pick does — place at
    `point`, auto-connect from a dropped handle, or retype `shapeId` in place. */
interface PickerRequest {
  mode: PickerMode;
  title: string;
  point?: Point;
  connect?: { sourceId: string; handleId: string };
  shapeId?: string;
}

interface WorkflowBoardProps {
  shapes: DiagramShape[];
  setShapes: Dispatch<SetStateAction<DiagramShape[]>>;
  /** History-tracked edit: App records the previous editor state, applies the
      updater, and lands the result. Live drag movement bypasses this (plain
      setShapes) so one drag is one undo step, not one per pointer event. */
  editShapes: (updater: (currentShapes: DiagramShape[]) => DiagramShape[]) => void;
  /** Stamps a finished drag into history: the moves themselves already landed
      through setShapes while dragging, so undo needs the pre-drag snapshot. */
  commitDrag: (preDragShapes: DiagramShape[]) => void;
  selectedId: string | null;
  setSelectedId: Dispatch<SetStateAction<string | null>>;
  /** A step sits on App's cross-workflow clipboard, so Paste step can appear. */
  canPasteStep: boolean;
  /** Step actions, implemented in App so inspector and board share them. */
  onDuplicateStep: (id: string) => void;
  onCopyStep: (id: string) => void;
  onPasteStep: (point: Point) => void;
  sessionControls?: (actions: { clearCanvas: () => void }) => ReactNode;
}
export function WorkflowBoard({
  shapes,
  setShapes,
  editShapes,
  commitDrag,
  selectedId,
  setSelectedId,
  canPasteStep,
  onDuplicateStep,
  onCopyStep,
  onPasteStep,
  sessionControls
}: WorkflowBoardProps) {
  const [picker, setPicker] = useState<PickerRequest | null>(null);
  const [dragStart, setDragStart] = useState<Point | null>(null);
  const [panStart, setPanStart] = useState<{ clientX: number; clientY: number; origin: Point } | null>(null);
  const [connectorDrag, setConnectorDrag] = useState<{ sourceId: string; sourceHandleId: string; start: Point; current: Point } | null>(null);
  const [reattachDrag, setReattachDrag] = useState<{ arrowId: string; endpoint: "source" | "target"; fixed: Point; current: Point } | null>(null);
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number; point: Point; shapeId?: string } | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingLabel, setEditingLabel] = useState("");
  const [canvasViewBox, setCanvasViewBox] = useState({ width: 0, height: 0 });
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState<Point>({ x: 0, y: 0 });
  const svgRef = useRef<SVGSVGElement | null>(null);
  const editorRef = useRef<HTMLInputElement | null>(null);
  const dragSnapshotRef = useRef<DiagramShape[] | null>(null);
  const didDragRef = useRef(false);
  const skipNextEditCommitRef = useRef(false);
  const pointersRef = useRef(new Map<number, Point>());
  const pinchRef = useRef<{ distance: number; zoom: number } | null>(null);
  const didFitRef = useRef(false);

  const selectedShape = shapes.find((shape) => shape.id === selectedId) ?? null;
  const editingShape = shapes.find((shape) => shape.id === editingId) ?? null;
  const canvasClass = useMemo(() => (
    ["drawing-surface", panStart ? "panning" : ""].filter(Boolean).join(" ")
  ), [panStart]);
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

  /** Discrete canvas ops (add, connect, delete, rename, reattach) go through
      App's history so board and inspector edits share one undo timeline. */
  function commitShapes(updater: (currentShapes: DiagramShape[]) => DiagramShape[]) {
    editShapes(updater);
  }

  /** Places a node (or note) at `point`; with `connect`, the same history
      step adds an arrow from that handle so "drop a handle, pick a step"
      costs one undo. Returns the new shape's id. */
  function addShape(point: Point, kind: PaletteKind, connect?: { sourceId: string; handleId: string }): string {
    const id = crypto.randomUUID();
    if (kind === "note") {
      const next: DiagramShape = {
        id,
        type: "note",
        x: point.x - 110,
        y: point.y - 44,
        width: 220,
        height: 88,
        label: "Note"
      };
      commitShapes((currentShapes) => [...currentShapes, next]);
    } else {
      const next: DiagramShape = {
        id,
        type: "node",
        x: point.x - NODE_WIDTH / 2,
        y: point.y - NODE_HEIGHT / 2,
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
        label: paletteLabel(kind),
        data: nodeDataForKind(kind)
      };
      commitShapes((currentShapes) => {
        if (!connect) return [...currentShapes, next];
        const source = currentShapes.find((shape) => shape.id === connect.sourceId);
        if (!isNodeShape(source) || next.data?.nodeKind === "trigger") return [...currentShapes, next];
        const sourceHandle = connectionHandleById(source, connect.handleId) ?? nearestConnectionHandle(source, point);
        const targetHandle = nearestConnectionHandle(next, point);
        const arrow: DiagramShape = {
          id: crypto.randomUUID(),
          type: "arrow",
          x: sourceHandle.x,
          y: sourceHandle.y,
          width: targetHandle.x - sourceHandle.x,
          height: targetHandle.y - sourceHandle.y,
          sourceId: source.id,
          targetId: next.id,
          sourceHandleId: sourceHandle.id,
          targetHandleId: targetHandle.id
        };
        return [...currentShapes, next, arrow];
      });
    }
    setSelectedId(id);
    return id;
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
  }

  function canvasCenter(): Point {
    return {
      x: zoomedViewBox.x + zoomedViewBox.width / 2,
      y: zoomedViewBox.y + zoomedViewBox.height / 2
    };
  }

  /** What a picker pick does: place at the request's point (or the canvas
      center), auto-connect when it came from a dropped handle, or retype the
      node in place for "change action type". Center placements cascade a
      little so consecutive adds don't stack exactly on top of each other. */
  function pickStep(kind: PaletteKind) {
    const request = picker;
    setPicker(null);
    if (!request) return;
    if (request.mode === "change" && request.shapeId) {
      changeActionType(request.shapeId, kind as ActionType);
      return;
    }
    let point = request.point;
    if (!point) {
      const cascade = shapes.length % 6;
      point = {
        x: canvasCenter().x + cascade * 26,
        y: canvasCenter().y + cascade * 20
      };
    }
    addShape(point, kind, request.connect);
  }

  function onPointerDown(event: React.PointerEvent<SVGSVGElement>) {
    pointersRef.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointersRef.current.size === 2) {
      const active = Array.from(pointersRef.current.values());
      setDragStart(null);
      setPanStart(null);
      setConnectorDrag(null);
      setReattachDrag(null);
      pinchRef.current = {
        distance: Math.hypot(active[0].x - active[1].x, active[0].y - active[1].y),
        zoom
      };
      event.currentTarget.setPointerCapture(event.pointerId);
      return;
    }

    const point = toCanvasPoint(event);
    const hit = findNodeAt(shapes, point);
    const connectorHit = hit ? undefined : findConnectorAt(shapes, point);

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
    if (pointersRef.current.has(event.pointerId)) {
      pointersRef.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    }
    const pinch = pinchRef.current;
    if (pinch && pointersRef.current.size >= 2) {
      const svg = svgRef.current;
      if (!svg) return;
      const rect = svg.getBoundingClientRect();
      const active = Array.from(pointersRef.current.values());
      const distance = Math.hypot(active[0].x - active[1].x, active[0].y - active[1].y);
      const midX = (active[0].x + active[1].x) / 2;
      const midY = (active[0].y + active[1].y) / 2;
      const nextZoom = Math.min(maxZoom, Math.max(minZoom, Math.round(((pinch.zoom * distance) / pinch.distance) * 100) / 100));
      const worldPerPx = canvasViewBox.width / zoom / rect.width;
      const midWorldX = canvasViewBox.width / 2 + pan.x + (midX - rect.left - rect.width / 2) * worldPerPx;
      const midWorldY = canvasViewBox.height / 2 + pan.y + (midY - rect.top - rect.height / 2) * worldPerPx;
      setZoom(nextZoom);
      setPan({ x: midWorldX - canvasViewBox.width / 2, y: midWorldY - canvasViewBox.height / 2 });
      return;
    }
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
    if (!selectedId || !dragStart) return;
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
    pointersRef.current.delete(event.pointerId);
    if (pointersRef.current.size < 2) pinchRef.current = null;
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
      if (isNodeShape(target)) {
        addConnector(connectorDrag.sourceId, connectorDrag.sourceHandleId, target, point);
      } else {
        // Dropped on empty canvas: the most common job is "add the next
        // step" — offer the picker and wire whatever lands to this handle.
        setPicker({
          mode: "action",
          title: "Add a step",
          point,
          connect: { sourceId: connectorDrag.sourceId, handleId: connectorDrag.sourceHandleId }
        });
      }
      setConnectorDrag(null);
      return;
    }
    if (didDragRef.current && dragSnapshotRef.current) {
      commitDrag(dragSnapshotRef.current);
    }
    setPanStart(null);
    dragSnapshotRef.current = null;
    didDragRef.current = false;
    setDragStart(null);
  }

  function changeZoom(delta: number) {
    setZoom((current) => Math.min(maxZoom, Math.max(minZoom, Math.round((current + delta) * 10) / 10)));
  }

  function fitToContent() {
    if (!shapes.length || canvasViewBox.width <= 0 || canvasViewBox.height <= 0) return;
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const shape of shapes) {
      // Arrows store placeholder rects at the origin; measure their real
      // endpoints instead.
      const box = shape.type === "arrow"
        ? (() => {
          const endpoints = connectorEndpoints(shape, shapes);
          return {
            left: Math.min(endpoints.start.x, endpoints.end.x),
            right: Math.max(endpoints.start.x, endpoints.end.x),
            top: Math.min(endpoints.start.y, endpoints.end.y),
            bottom: Math.max(endpoints.start.y, endpoints.end.y)
          };
        })()
        : {
          left: Math.min(shape.x, shape.x + shape.width),
          right: Math.max(shape.x, shape.x + shape.width),
          top: Math.min(shape.y, shape.y + shape.height),
          bottom: Math.max(shape.y, shape.y + shape.height)
        };
      minX = Math.min(minX, box.left);
      minY = Math.min(minY, box.top);
      maxX = Math.max(maxX, box.right);
      maxY = Math.max(maxY, box.bottom);
    }
    const pad = 48;
    const nextZoom = Math.min(
      1,
      Math.max(
        minZoom,
        Math.round(Math.min(
          canvasViewBox.width / (maxX - minX + pad * 2),
          canvasViewBox.height / (maxY - minY + pad * 2)
        ) * 100) / 100
      )
    );
    setZoom(nextZoom);
    setPan({
      x: minX + (maxX - minX) / 2 - canvasViewBox.width / 2,
      y: minY + (maxY - minY) / 2 - canvasViewBox.height / 2
    });
  }

  function deleteSelected() {
    if (!selectedId) return;
    commitShapes((currentShapes) => currentShapes.filter((shape) => (
      shape.id !== selectedId && shape.sourceId !== selectedId && shape.targetId !== selectedId
    )));
    setSelectedId(null);
    setContextMenu(null);
  }

  function clearShapes() {
    if (shapes.length && !window.confirm("Clear the canvas? This removes all nodes and connectors.")) return;
    commitShapes(() => []);
    setSelectedId(null);
    setConnectorDrag(null);
    setReattachDrag(null);
    setContextMenu(null);
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
    if (findNodeAt(shapes, point) || findConnectorAt(shapes, point)) return;
    event.preventDefault();
    setPicker({ mode: "action", title: "Add a step", point });
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
        label: actionMeta(actionType)?.label ?? actionType,
        data: { ...defaultNodeData("action"), actionType, fields: defaultFields(actionType) }
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
      // Delete/Backspace and Ctrl+Z/Ctrl+Y live in App's editor-wide handler;
      // this one only closes the board's own surfaces.
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

  // Delete/undo in App can remove the shape a context menu points at; drop
  // the menu instead of leaving actions that would act on nothing.
  useEffect(() => {
    if (contextMenu?.shapeId && !shapes.some((shape) => shape.id === contextMenu.shapeId)) {
      setContextMenu(null);
    }
  });

  useEffect(() => {
    const currentSvg = svgRef.current;
    if (!currentSvg) return;
    const observedSvg = currentSvg;

    function updateViewBox() {
      const rect = observedSvg.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return;
      const nextViewBox = { width: Math.round(rect.width), height: Math.round(rect.height) };
      setCanvasViewBox((current) => (
        current.width === nextViewBox.width && current.height === nextViewBox.height ? current : nextViewBox
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

  useEffect(() => {
    if (didFitRef.current || !shapes.length || canvasViewBox.width <= 0 || canvasViewBox.height <= 0) return;
    didFitRef.current = true;
    fitToContent();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shapes, canvasViewBox]);

  const editorPosition = editingShape ? toViewportPoint(centerOf(editingShape)) : null;
  const editorFontSize = toViewportFontSize();
  const editorWidth = Math.min(280, Math.max(60, editingLabel.length * editorFontSize * 0.62 + 18));
  const contextShape = contextMenu?.shapeId ? shapes.find((shape) => shape.id === contextMenu.shapeId) : null;

  return (
    <section className="board-panel" aria-label="Workflow board">
      <div className="board-toolbar">
        {/* The palette collapsed into three intents: when the workflow runs
            (trigger), what it does (step), and a free-form note. Everything
            else lives in the searchable picker those buttons open. */}
        <div className="palette-actions" aria-label="Add nodes">
          <button
            className="palette-button"
            onClick={() => setPicker({ mode: "trigger", title: "When should this run?" })}
            title="Add a trigger — when the workflow runs"
            type="button"
          >
            <Zap size={16} strokeWidth={1.5} />
            <span>Trigger</span>
          </button>
          <button
            className="palette-button accent"
            onClick={() => setPicker({ mode: "action", title: "Add a step" })}
            title="Add a step — flow control, AI, developer tools, or an app"
            type="button"
          >
            <Plus size={16} strokeWidth={1.5} />
            <span>Step</span>
          </button>
          <button
            className="palette-button"
            onClick={() => addShape(canvasCenter(), "note")}
            title="Add a note"
            type="button"
          >
            <StickyNote size={16} strokeWidth={1.5} />
            <span>Note</span>
          </button>
        </div>
        {sessionControls && (
          <div className="canvas-session-controls">
            {sessionControls({ clearCanvas: clearShapes })}
          </div>
        )}
      </div>

      <div className="canvas-zoom-controls" aria-label="Canvas zoom controls">
        <button className="icon-button" onClick={() => changeZoom(-0.1)} disabled={zoom <= minZoom} title="Zoom out" type="button">
          <Minus size={16} strokeWidth={1.5} />
        </button>
        <button className="zoom-button" onClick={() => { setZoom(1); setPan({ x: 0, y: 0 }); }} title="Reset zoom" type="button">
          {Math.round(zoom * 100)}%
        </button>
        <button className="icon-button" onClick={() => changeZoom(0.1)} disabled={zoom >= maxZoom} title="Zoom in" type="button">
          <Plus size={16} strokeWidth={1.5} />
        </button>
        <button className="icon-button" onClick={fitToContent} title="Fit to view" type="button">
          <Maximize size={16} strokeWidth={1.5} />
        </button>
      </div>

      <svg
        ref={svgRef}
        className={canvasClass}
        viewBox={`${zoomedViewBox.x} ${zoomedViewBox.y} ${zoomedViewBox.width} ${zoomedViewBox.height}`}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onDoubleClick={onCanvasDoubleClick}
        onContextMenu={onContextMenu}
        preserveAspectRatio="none"
      >
        <defs>
          <pattern id="grid" width="28" height="28" patternUnits="userSpaceOnUse">
            <path d="M 28 0 L 0 0 0 28" fill="none" stroke="var(--dk-border-default)" strokeWidth="1" />
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
            const Icon = nodeIcon(shape.data);
            const title = nodeTitle(shape);
            const subtitle = nodeSubtitle(shape);
            return (
              <g key={shape.id} onDoubleClick={(event) => { event.stopPropagation(); openEditor(shape); }}>
                <rect
                  x={shape.x}
                  y={shape.y}
                  width={shape.width}
                  height={shape.height}
                  rx="6"
                  fill="var(--dk-bg-surface)"
                  stroke={color}
                  strokeWidth={strokeWidth}
                />
                <rect x={shape.x + 14} y={shape.y + (shape.height - 36) / 2} width="36" height="36" rx="5" fill="var(--dk-accent-soft)" />
                <Icon
                  size={22}
                  x={shape.x + 21}
                  y={shape.y + (shape.height - 36) / 2 + 7}
                  color={color}
                  strokeWidth={1.5}
                />
                <text x={shape.x + 62} y={shape.y + (subtitle ? 40 : 53)} fill="var(--dk-text-primary)" fontSize={subtitle ? 15 : 13} fontWeight="500">
                  <title>{shape.label}</title>
                  {displayLabel(title)}
                </text>
                {subtitle && (
                  <text className="node-subtitle" x={shape.x + 62} y={shape.y + 62} fill="var(--dk-text-muted)" fontSize="12">
                    {displayLabel(subtitle)}
                  </text>
                )}
              </g>
            );
          }

          if (shape.type === "note") {
            return (
              <g key={shape.id} onDoubleClick={(event) => { event.stopPropagation(); openEditor(shape); }}>
                <rect x={shape.x} y={shape.y} width={shape.width} height={shape.height} rx="4" fill="var(--dk-warning-bg)" stroke={noteColor} strokeWidth={strokeWidth} />
                <text x={shape.x + 14} y={shape.y + 30} fill="var(--dk-text-primary)" fontSize={shapeLabelSize} fontWeight="500">
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
                  <circle className="connection-handle" cx={endpoints.start.x} cy={endpoints.start.y} r="8" fill={handleColor} stroke="var(--dk-bg-surface)" strokeWidth="3"
                    onPointerDown={(event) => startReattachDrag(event, shape, "source")} />
                  <circle className="connection-handle" cx={endpoints.end.x} cy={endpoints.end.y} r="8" fill={handleColor} stroke="var(--dk-bg-surface)" strokeWidth="3"
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
            stroke="var(--dk-bg-surface)"
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
          {!contextMenu.shapeId && (
            <button onClick={() => { setPicker({ mode: "action", title: "Add a step", point: contextMenu.point }); setContextMenu(null); }} type="button">
              <ListPlus size={16} />
              Add step…
            </button>
          )}
          <button onClick={() => { addShape(contextMenu.point, "note"); setContextMenu(null); }} type="button">
            <StickyNote size={16} />
            Add note
          </button>
          {contextShape && contextShape.type === "node" && contextShape.data?.nodeKind === "action" && (
            <div className="context-menu-group">
              <button onClick={() => { setPicker({ mode: "change", title: "Change action type", shapeId: contextMenu.shapeId }); setContextMenu(null); }} type="button">
                <ListRestart size={16} />
                Change action type…
              </button>
              <button onClick={() => { onDuplicateStep(contextMenu.shapeId!); setContextMenu(null); }} type="button">
                <Copy size={16} />
                Duplicate
              </button>
              <button onClick={() => { onCopyStep(contextMenu.shapeId!); setContextMenu(null); }} type="button">
                <ClipboardCopy size={16} />
                Copy step
              </button>
            </div>
          )}
          {canPasteStep && (
            <button onClick={() => { onPasteStep({ x: contextMenu.point.x + 24, y: contextMenu.point.y + 24 }); setContextMenu(null); }} type="button">
              <ClipboardPaste size={16} />
              Paste step
            </button>
          )}
          {contextMenu.shapeId && (
            <button className="danger" onClick={deleteSelected} disabled={selectedId !== contextMenu.shapeId} type="button">
              <Trash2 size={16} />
              Delete
            </button>
          )}
        </div>
      )}

      {picker && (
        <StepPicker
          mode={picker.mode}
          title={picker.title}
          onPick={pickStep}
          onClose={() => setPicker(null)}
        />
      )}
    </section>
  );
}
