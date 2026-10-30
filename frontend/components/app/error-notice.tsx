import { AlertTriangle } from "lucide-react";

export function ErrorNotice({ message }: { message: string }) {
  return (
    <p
      role="alert"
      className="flex items-start gap-2 rounded-md border border-destructive/30 bg-red-50 px-3 py-2 text-sm font-medium text-destructive"
    >
      <AlertTriangle className="mt-0.5 size-4 shrink-0" />
      {message}
    </p>
  );
}
