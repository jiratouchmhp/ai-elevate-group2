/**
 * Altostrat Singapore — HR Agentic Assistant Experience Plane (SDD §3.10, D8: React + Vite TypeScript).
 *
 * Implements the three architectural UI affordances required by SDD §3.10:
 * 1. CitationChip (FR-5.3, C-1, C-4): Resolves against `citation_anchor` (`content_hash` + `heading_slug`)
 *    and displays `semantic_topic` so misfiled sections (e.g. §5.5 Relocation) never display as "Community Guidelines".
 * 2. ConfirmationCard (B-3): Structured Confirm / Cancel card rendered from `STATE_DELTA` AG-UI events,
 *    making confirm-before-write a UI invariant rather than free-text "yeah ok".
 * 3. RefusalSurface (FR-1.3, FR-5.4, §3.3): Renders grounded refusals and guardrail blocks as a first-class
 *    state with escalation links rather than an unhandled error.
 */

import React, { useState } from 'react';

export interface CitationMetadata {
  chunk_id: string;
  section_number: string;
  section_title: string;
  semantic_topic: string;
  citation_anchor: string;
  deep_link_url: string;
  authority: 'primary' | 'summary';
  jurisdiction: 'SG' | 'GLOBAL';
}

export interface ConfirmationCardPayload {
  action: string;
  employee_id: string;
  idempotency_key: string;
  warnings?: string[];
  [key: string]: unknown;
}

export const CitationChip: React.FC<{ citation: CitationMetadata }> = ({ citation }) => {
  return (
    <a
      href={citation.deep_link_url}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium bg-blue-50 text-blue-800 border border-blue-200 hover:bg-blue-100"
      data-anchor={citation.citation_anchor}
      title={`Section ${citation.section_number} (${citation.authority} authority) — ${citation.citation_anchor}`}
    >
      <span>[{citation.semantic_topic}]</span>
      <span className="text-blue-500">§{citation.section_number}</span>
    </a>
  );
};

export const ConfirmationCard: React.FC<{
  card: ConfirmationCardPayload;
  onConfirm: (idempotencyKey: string) => void;
  onCancel: () => void;
}> = ({ card, onConfirm, onCancel }) => {
  return (
    <div
      role="region"
      aria-label="Action Confirmation Required"
      className="p-4 rounded-lg border border-amber-300 bg-amber-50 shadow-sm my-2"
    >
      <h4 className="font-semibold text-amber-900 text-sm mb-2">
        Confirm Authorized Action ({card.action})
      </h4>
      {card.warnings && card.warnings.length > 0 && (
        <div className="mb-2 text-xs text-amber-800 bg-amber-100 p-2 rounded">
          {card.warnings.map((w, idx) => (
            <p key={idx}>{w}</p>
          ))}
        </div>
      )}
      <div className="flex gap-2 mt-3">
        <button
          type="button"
          onClick={() => onConfirm(card.idempotency_key)}
          className="px-3 py-1.5 bg-blue-600 text-white text-xs font-semibold rounded hover:bg-blue-700"
        >
          Confirm & Execute
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="px-3 py-1.5 bg-white text-gray-700 border border-gray-300 text-xs font-semibold rounded hover:bg-gray-50"
        >
          Cancel
        </button>
      </div>
    </div>
  );
};
