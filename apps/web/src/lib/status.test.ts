import { describe, expect, it } from 'vitest';
import { PIPELINE_STAGES } from '@pdf2x/contracts';
import {
  TOTAL_WEIGHT,
  fractionWithinStage,
  overallProgress,
  progressForStatus,
  stageRange,
  stepStateFor,
} from '@/lib/status';

describe('pipeline weights', () => {
  it('sums to 1', () => {
    expect(TOTAL_WEIGHT).toBeCloseTo(1, 6);
    expect(PIPELINE_STAGES).toHaveLength(8);
  });

  it('computes the cumulative range of each stage', () => {
    expect(stageRange(0)).toMatchObject({ start: 0 });
    expect(stageRange(0).end).toBeCloseTo(0.05);
    expect(stageRange(2).start).toBeCloseTo(0.15);
    expect(stageRange(2).end).toBeCloseTo(0.4);
    expect(stageRange(7).end).toBeCloseTo(1);
  });

  it('maps status to the weighted floor', () => {
    expect(progressForStatus('received', 0)).toBeCloseTo(0);
    expect(progressForStatus('validating', 0)).toBeCloseTo(0);
    expect(progressForStatus('queued', 0)).toBeCloseTo(0.05);
    expect(progressForStatus('extracting', 0.01)).toBeCloseTo(0.15);
    expect(progressForStatus('generating', 0)).toBeCloseTo(0.75);
  });

  it('never regresses below the floor and respects reported progress above it', () => {
    expect(progressForStatus('extracting', 0.28)).toBeCloseTo(0.28);
    expect(progressForStatus('extracting', 0.9)).toBeCloseTo(0.9);
    expect(progressForStatus('extracting', 2)).toBeCloseTo(1);
  });

  it('clamps terminal statuses', () => {
    expect(progressForStatus('completed', 0.5)).toBe(1);
    expect(progressForStatus('completed_with_warnings', 0)).toBe(1);
    expect(progressForStatus('failed', 0.42)).toBeCloseTo(0.42);
    expect(progressForStatus('cancelled', 0.2)).toBeCloseTo(0.2);
    expect(overallProgress({ status: 'completed', progress: 0 })).toBe(1);
  });

  it('is monotonic across the ordered active statuses', () => {
    const ordered = [
      'received',
      'validating',
      'queued',
      'inspecting',
      'extracting',
      'structuring',
      'normalizing',
      'reconciling',
      'generating',
      'auditing',
      'completed',
    ] as const;
    let previous = -1;
    for (const status of ordered) {
      const value = progressForStatus(status, 0);
      expect(value).toBeGreaterThanOrEqual(previous);
      previous = value;
    }
  });

  it('derives the fraction inside a stage', () => {
    expect(fractionWithinStage(2, 0.15)).toBeCloseTo(0);
    expect(fractionWithinStage(2, 0.275)).toBeCloseTo(0.5);
    expect(fractionWithinStage(2, 0.4)).toBeCloseTo(1);
    expect(fractionWithinStage(2, 0.05)).toBeCloseTo(0);
  });
});

describe('stepper states', () => {
  it('marks previous stages done, current as current and the rest pending', () => {
    expect(stepStateFor(0, 'extracting', 'extract')).toBe('done');
    expect(stepStateFor(1, 'extracting', 'extract')).toBe('done');
    expect(stepStateFor(2, 'extracting', 'extract')).toBe('current');
    expect(stepStateFor(3, 'extracting', 'extract')).toBe('pending');
  });

  it('marks every step done on success', () => {
    for (let index = 0; index < PIPELINE_STAGES.length; index += 1) {
      expect(stepStateFor(index, 'completed', 'done')).toBe('done');
    }
  });

  it('pinpoints the failed stage', () => {
    expect(stepStateFor(1, 'failed', 'extract')).toBe('done');
    expect(stepStateFor(2, 'failed', 'extract')).toBe('failed');
    expect(stepStateFor(3, 'failed', 'extract')).toBe('pending');
  });

  it('falls back to the first stage when the failure stage is unknown', () => {
    expect(stepStateFor(0, 'failed', 'failed')).toBe('failed');
    expect(stepStateFor(1, 'failed', 'failed')).toBe('pending');
  });
});
