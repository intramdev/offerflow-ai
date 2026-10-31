"use client";

import { Star } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/cn";

export function StarRating({
  value,
  onChange,
  label,
  disabled,
}: {
  value: number | null;
  onChange?: (stars: number | null) => void;
  label: string;
  disabled?: boolean;
}) {
  const interactive = !!onChange && !disabled;
  return (
    <div
      className="flex items-center gap-0.5"
      role={interactive ? "radiogroup" : "img"}
      aria-label={
        value ? `${label}: ${value} of 5 stars` : `${label}: not rated`
      }
    >
      {[1, 2, 3, 4, 5].map((stars) =>
        interactive ? (
          <button
            key={stars}
            type="button"
            role="radio"
            aria-checked={value === stars}
            aria-label={`${stars} star${stars > 1 ? "s" : ""}`}
            title={value === stars ? "Clear rating" : `${stars} stars`}
            onClick={() => onChange(value === stars ? null : stars)}
            className="rounded p-0.5 text-amber-500 hover:scale-110 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-primary"
          >
            <Star
              className={cn(
                "size-5",
                value !== null && stars <= value
                  ? "fill-amber-400 stroke-amber-500"
                  : "stroke-muted-foreground",
              )}
            />
          </button>
        ) : (
          <Star
            key={stars}
            className={cn(
              "size-4",
              value !== null && stars <= value
                ? "fill-amber-400 stroke-amber-500"
                : "stroke-muted-foreground",
            )}
          />
        ),
      )}
    </div>
  );
}
