import { Badge } from "@/components/ui/badge";

function variantFor(status: string): "default" | "secondary" | "destructive" | "warning" | "outline" {
  switch (status) {
    case "completed":
    case "approved":
    case "ready":
    case "ok":
      return "default";
    case "failed":
    case "excluded":
      return "destructive";
    case "needs_input":
    case "needs_review":
    case "validation":
    case "interpretation":
    case "parsing":
    case "uploaded":
      return "warning";
    default:
      return "secondary";
  }
}

export function StatusBadge({ status }: { status: string }) {
  return <Badge variant={variantFor(status)}>{status.replace(/_/g, " ")}</Badge>;
}
