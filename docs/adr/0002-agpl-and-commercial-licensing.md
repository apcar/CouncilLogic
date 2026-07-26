# ADR 0002: AGPL public core and optional commercial licensing

- Status: Accepted
- Date: 2026-07-26

## Context

CouncilLogic is a portable, governed council control plane. The licensing
choice should preserve software freedom for users of modified network-served
versions while leaving room for separately negotiated proprietary use where
the copyright holder has sufficient rights.

This record is general information, not legal advice. Contribution terms and
any commercial offering require appropriate legal review.

## Decision

This public repository begins from a clean repository root under the GNU
Affero General Public License version 3 only, with SPDX identifier
`AGPL-3.0-only`. Earlier private development history is outside this
repository. This statement describes the provenance boundary of the public
repository; it does not claim that any public history was rewritten.

The copyright holder may later offer a separate commercial license for
proprietary embedding or other negotiated needs. No commercial terms currently
exist, and this decision is not an offer or commitment to provide them.

External copyrightable contributions will not be accepted until a
counsel-reviewed contributor license agreement grants sufficient rights for
this strategy.

## Scope and limitations

- The AGPL permits use, study, modification, self-hosting, and commercial use
  when its terms are followed.
- Modified versions made available for remote network interaction trigger the
  section 13 duty to offer Corresponding Source to those remote users, along
  with any other applicable AGPL obligations.
- The AGPL does not prohibit compliant hosted competitors.
- A separate commercial license would be an alternative negotiated agreement,
  not a restriction on rights already granted for AGPL-licensed versions.
- Copyright licensing does not grant trademark rights.
- This ADR does not launch a hosted service, publish a package, or offer
  commercial terms.

## Consequences

The public core can be used commercially under the AGPL. Operators of modified
network-served versions must address corresponding-source duties. Proprietary
embedding or other needs may be discussed only if separate terms are later
created and the project has the rights needed to grant them.

External contributions remain closed until the contribution gate is opened.
