import type { DiagramShape, Point } from "../types";

export type ConnectionHandle = { id: string; x: number; y: number };

export const nodeColor = "var(--diagram-component)";
export const triggerColor = "var(--diagram-trigger)";
export const noteColor = "var(--diagram-note)";
export const handleColor = "var(--diagram-handle)";
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
