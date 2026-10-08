import type { DiagramShape, Point } from "../types";

export type ConnectionHandle = { id: string; x: number; y: number };

export const nodeColor = "var(--dk-accent-default)";
export const triggerColor = "var(--dk-warning-text)";
export const noteColor = "var(--dk-warning-text)";
export const handleColor = "var(--dk-accent-default)";
export const shapeLabelSize = 16;
export const minZoom = 0.25;
export const maxZoom = 2;
export function shapeColor(shape: DiagramShape) {
  if (shape.type === "note") return noteColor;
  if (shape.data?.nodeKind === "trigger") return triggerColor;
  return nodeColor;
}

export function centerOf(shape: DiagramShape): Point {
  return { x: shape.x + shape.width / 2, y: shape.y + shape.height / 2 };
}

export function shapeContains(shape: DiagramShape, point: Point) {
  return point.x >= shape.x - 10
    && point.x <= shape.x + shape.width + 10
    && point.y >= shape.y - 10
    && point.y <= shape.y + shape.height + 10;
}

export function findNodeAt(shapes: DiagramShape[], point: Point) {
  return [...shapes].reverse().find((shape) => shape.type !== "arrow" && shapeContains(shape, point));
}

export function isNodeShape(shape: DiagramShape | null | undefined): shape is DiagramShape {
  return Boolean(shape && shape.type === "node");
}

export function distanceToSegment(point: Point, start: Point, end: Point) {
  const dx = end.x - start.x;
  const dy = end.y - start.y;
  const lengthSquared = dx * dx + dy * dy;
  if (lengthSquared === 0) return Math.hypot(point.x - start.x, point.y - start.y);
  const t = Math.max(0, Math.min(1, ((point.x - start.x) * dx + (point.y - start.y) * dy) / lengthSquared));
  const projection = { x: start.x + t * dx, y: start.y + t * dy };
  return Math.hypot(point.x - projection.x, point.y - projection.y);
}

export function displayLabel(label: string) {
  return label.length > 26 ? `${label.slice(0, 23)}...` : label;
}

export function connectionHandles(shape: DiagramShape): ConnectionHandle[] {
  return [
    { id: "top", x: shape.x + shape.width / 2, y: shape.y },
    { id: "right", x: shape.x + shape.width, y: shape.y + shape.height / 2 },
    { id: "bottom", x: shape.x + shape.width / 2, y: shape.y + shape.height },
    { id: "left", x: shape.x, y: shape.y + shape.height / 2 }
  ];
}

export function nearestConnectionHandle(shape: DiagramShape, point: Point) {
  return connectionHandles(shape).reduce((nearest, handle) => (
    Math.hypot(point.x - handle.x, point.y - handle.y) < Math.hypot(point.x - nearest.x, point.y - nearest.y)
      ? handle
      : nearest
  ));
}

export function connectionHandleById(shape: DiagramShape, handleId: string | undefined) {
  return connectionHandles(shape).find((handle) => handle.id === handleId);
}

export function inferConnectionHandles(source: DiagramShape, target: DiagramShape) {
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

export function connectorEndpoints(shape: DiagramShape, shapes: DiagramShape[]) {
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

export function refreshConnectedArrow(shape: DiagramShape, shapes: DiagramShape[]) {
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

export function refreshArrowsForMovedShape(shapes: DiagramShape[], movedShapeId: string) {
  return shapes.map((shape) => (
    shape.sourceId === movedShapeId || shape.targetId === movedShapeId
      ? refreshConnectedArrow(shape, shapes)
      : shape
  ));
}

export function findConnectorAt(shapes: DiagramShape[], point: Point) {
  return [...shapes].reverse().find((shape) => {
    if (shape.type !== "arrow") return false;
    const endpoints = connectorEndpoints(shape, shapes);
    return distanceToSegment(point, endpoints.start, endpoints.end) <= 12;
  });
}

/** The viewport a workflow opens at: 100% zoom (never shrunk to fit — the
    wheel pans to the rest), with the first trigger (else the topmost node)
    centered horizontally and `margin` world units below the top edge.
    `world` is the unzoomed view box. */
export function openingViewport(shapes: DiagramShape[], world: { width: number; height: number }, margin = 48): Viewport {
  const nodes = shapes.filter((shape) => shape.type === "node" || shape.type === "note");
  const byPosition = (a: DiagramShape, b: DiagramShape) => a.y - b.y || a.x - b.x;
  const anchor = nodes.filter((shape) => shape.data?.nodeKind === "trigger").sort(byPosition)[0]
    ?? [...nodes].sort(byPosition)[0];
  if (!anchor) return { zoom: 1, pan: { x: 0, y: 0 } };
  const top = Math.min(anchor.y, anchor.y + anchor.height);
  return {
    zoom: 1,
    pan: { x: centerOf(anchor).x - world.width / 2, y: top - margin }
  };
}

/** The slice of a WheelEvent the canvas reads. */
export interface WheelInput {
  deltaX: number;
  deltaY: number;
  /** 0 = pixels, 1 = lines, 2 = pages (WheelEvent.DOM_DELTA_*). */
  deltaMode: number;
  ctrlKey: boolean;
  metaKey: boolean;
  shiftKey: boolean;
}

export interface Viewport {
  zoom: number;
  pan: Point;
}

/** Wheel gestures the way node editors (Figma, n8n, Miro) read them:
    - pinch (browsers send it as ctrl+wheel) and Ctrl/Cmd+wheel zoom;
    - a mouse wheel notch (line mode, or a large whole-pixel vertical step
      with no horizontal part) zooms;
    - shift+wheel and trackpad two-finger scrolls (fractional, small or
      horizontal pixel deltas) pan. */
export function wheelGesture(input: WheelInput): "zoom" | "pan" {
  if (input.ctrlKey || input.metaKey) return "zoom";
  if (input.shiftKey) return "pan";
  if (input.deltaMode !== 0) return "zoom";
  if (input.deltaX !== 0) return "pan";
  return Number.isInteger(input.deltaY) && Math.abs(input.deltaY) >= 50 ? "zoom" : "pan";
}

/** Next viewport for one wheel event. `offset` is the pointer's position
    relative to the canvas center in screen px, `size` the canvas size in px,
    `world` the unzoomed view box — the WorkflowBoard model, where the visible
    box is world/zoom centered on world/2 + pan. Zoom keeps the world point
    under the pointer fixed and clamps to minZoom..maxZoom; pan moves the
    view by the scroll delta in screen px. */
export function wheelViewport(
  view: Viewport,
  input: WheelInput,
  offset: Point,
  size: { width: number; height: number },
  world: { width: number; height: number }
): Viewport {
  if (size.width <= 0 || size.height <= 0 || world.width <= 0 || world.height <= 0) return view;
  const unit = input.deltaMode === 1 ? 16 : input.deltaMode === 2 ? size.height : 1;
  const dx = input.deltaX * unit;
  const dy = input.deltaY * unit;
  const perPxX = world.width / view.zoom / size.width;
  const perPxY = world.height / view.zoom / size.height;
  if (wheelGesture(input) === "pan") {
    // Shift+wheel on a mouse scrolls sideways; some browsers already swap
    // the axes for it, so take whichever delta is set.
    const [panX, panY] = input.shiftKey && dx === 0 ? [dy, 0] : [dx, dy];
    return { zoom: view.zoom, pan: { x: view.pan.x + panX * perPxX, y: view.pan.y + panY * perPxY } };
  }
  // Pinch deltas are small and frequent, wheel notches large: a steeper
  // curve for small steps keeps both feeling proportional.
  const rate = Math.abs(dy) < 50 ? 0.01 : 0.0015;
  const delta = Math.max(-100, Math.min(100, dy));
  const zoom = Math.min(maxZoom, Math.max(minZoom, view.zoom * Math.exp(-delta * rate)));
  if (zoom === view.zoom) return view;
  const nextPerPxX = world.width / zoom / size.width;
  const nextPerPxY = world.height / zoom / size.height;
  return {
    zoom,
    pan: {
      x: view.pan.x + offset.x * (perPxX - nextPerPxX),
      y: view.pan.y + offset.y * (perPxY - nextPerPxY)
    }
  };
}
