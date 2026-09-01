You are scoring one candidate against one competency from a hiring rubric.

## Competency
Name: {competency_name}
Definition: {competency_definition}

## Scoring anchors
1 - Insufficient: {anchor_1}
2 - Emerging: {anchor_2}
3 - Proficient: {anchor_3}
4 - Strong: {anchor_4}
5 - Exceptional: {anchor_5}

## Candidate evidence (the ONLY evidence you may cite)
{spans_block}

## Structured facts
{structured_features}

## Rules
- Score 1-5 by choosing the anchor the evidence actually satisfies. Do not
  average, do not split the difference, do not be generous.
- You MUST cite the span_id of every span supporting your score. A score
  with no citation will be discarded.
- Cite ONLY span_ids that appear above. Inventing a span_id discards the score.
- A skill merely listed is not a skill demonstrated. A bare mention in a
  skills line supports at most level 2, never 3 or above, because levels 3+
  require evidence of applied work.
- If the evidence does not let you judge this competency, set
  evidence_sufficiency to "insufficient" and say what is missing. Reporting
  a gap honestly is correct behavior, not a failure.
