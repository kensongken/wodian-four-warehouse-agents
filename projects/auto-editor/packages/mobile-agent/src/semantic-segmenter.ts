import type { LibraryAsset, UntypedSegment } from "@wearable/contracts";

import type { ContentLibrary } from "./content-library";

export type BoundarySignal =
  | "topic-change"
  | "speaker-change"
  | "action-change"
  | "product-change"
  | "pause";

export type BoundarySuggestion = Readonly<{
  startMs: number;
  endMs: number;
  transcript: string;
  signals: readonly BoundarySignal[];
}>;

export interface SegmentationAnalyzer {
  analyze(asset: LibraryAsset): Promise<readonly BoundarySuggestion[]>;
}

export async function segmentNewBatch(
  batchId: string,
  assets: readonly LibraryAsset[],
  analyzer: SegmentationAnalyzer,
  library: ContentLibrary,
): Promise<UntypedSegment[]> {
  const segments: UntypedSegment[] = [];

  for (const asset of assets) {
    if (asset.batchId !== batchId) {
      throw new Error(`asset-batch-mismatch:${asset.id}`);
    }

    if (asset.mediaKind !== "video") {
      segments.push(toSegment(asset, 0, 1, ""));
      continue;
    }

    let suggestions: readonly BoundarySuggestion[];
    try {
      suggestions = await analyzer.analyze(asset);
    } catch {
      await library.putAsset({
        ...asset,
        technicalFlags: unique([...asset.technicalFlags, "segmentation-failed"]),
      });
      continue;
    }

    assertValidBoundaries(suggestions);
    suggestions.forEach((suggestion, index) => {
      segments.push(
        toSegment(
          asset,
          suggestion.startMs,
          suggestion.endMs,
          suggestion.transcript,
          index,
        ),
      );
    });
  }

  await library.putUntypedSegments(segments);
  return segments;
}

function assertValidBoundaries(
  suggestions: readonly BoundarySuggestion[],
): void {
  let previousEnd = -1;
  for (const suggestion of suggestions) {
    if (
      !Number.isInteger(suggestion.startMs) ||
      !Number.isInteger(suggestion.endMs) ||
      suggestion.startMs < 0 ||
      suggestion.endMs <= suggestion.startMs
    ) {
      throw new Error("non-positive-boundary");
    }
    if (suggestion.startMs < previousEnd) {
      throw new Error("overlapping-boundaries");
    }
    previousEnd = suggestion.endMs;
  }
}

function toSegment(
  asset: LibraryAsset,
  startMs: number,
  endMs: number,
  transcript: string,
  index = 0,
): UntypedSegment {
  return {
    id: `segment_${asset.id}_${index}_${startMs}_${endMs}`,
    assetId: asset.id,
    batchId: asset.batchId,
    startMs,
    endMs,
    permissionState: asset.permissionState,
    transcript,
    technicalFlags: [...asset.technicalFlags],
  };
}

function unique(values: string[]): string[] {
  return [...new Set(values)];
}
