import type { IconProps } from "./catalog";

/**
 * Product logos as inline SVG marks, sized like lucide icons so they drop
 * into palette chips, canvas nodes and list rows interchangeably. Paths are
 * the canonical brand glyphs (simple-icons); the envelope is drawn by hand.
 */

interface BrandPath {
  d: string;
  fill: string;
}

function brandMark(title: string, paths: BrandPath[]) {
  return function BrandMark({ size = 16, className, x, y }: IconProps) {
    return (
      <svg
        width={size}
        height={size}
        viewBox="0 0 24 24"
        role="img"
        aria-label={title}
        className={className}
        x={x}
        y={y}
      >
        {paths.map((path) => <path key={path.d} d={path.d} fill={path.fill} />)}
      </svg>
    );
  };
}

export const SlackLogo = brandMark("Slack", [
  {
    // Bottom-left arm
    d: "M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zM6.313 15.165a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313z",
    fill: "#E01E5A"
  },
  {
    // Top-left arm
    d: "M8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zM8.834 6.313a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312z",
    fill: "#36C5F0"
  },
  {
    // Top-right arm
    d: "M18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zM17.688 8.834a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312z",
    fill: "#2EB67D"
  },
  {
    // Bottom-right arm
    d: "M15.165 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zM15.165 17.688a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z",
    fill: "#ECB22E"
  }
]);

export const YouTubeLogo = brandMark("YouTube", [
  {
    d: "M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814zM9.545 15.568V8.432L15.818 12l-6.273 3.568z",
    fill: "#FF0000"
  }
]);

export const DropboxLogo = brandMark("Dropbox", [
  {
    d: "M6 1.807 0 5.629l6 3.822 6.001-3.822L6 1.807zM18 1.807l-6 3.822 6 3.822 6-3.822-6-3.822zM0 13.274l6 3.822 6.001-3.822L6 9.452l-6 3.822zM18 9.452l-6 3.822 6 3.822 6-3.822-6-3.822zM6 18.371l6.001 3.822 6-3.822-6-3.822L6 18.371z",
    fill: "#0061FF"
  }
]);

/** Envelope mark for the email connector (Gmail red). */
export function MailLogo({ size = 16, className, x, y }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      role="img"
      aria-label="Email"
      className={className}
      x={x}
      y={y}
    >
      <path
        d="M3.6 3.9h16.8a2.4 2.4 0 0 1 2.4 2.4v11.4a2.4 2.4 0 0 1-2.4 2.4H3.6a2.4 2.4 0 0 1-2.4-2.4V6.3a2.4 2.4 0 0 1 2.4-2.4z"
        fill="#EA4335"
      />
      <path
        d="M2.9 6.4 12 13.3l9.1-6.9"
        fill="none"
        stroke="#fff"
        strokeWidth="1.9"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
