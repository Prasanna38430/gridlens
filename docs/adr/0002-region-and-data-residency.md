# ADR-0002: eu-west-3 for everything that stores data

Status: accepted
Date: 2026-08-11

## Context

I have to pick a region before the first bucket exists, and the choice is
close to irreversible. Moving an Iceberg warehouse between regions later means
copying every file, rewriting every manifest path, and re-registering every
table in a new Glue catalog. So this is worth ten minutes of thought now.

The obvious framing is data residency, and I want to be honest about it: it
does not apply here. ENTSO-E and RTE publish aggregate grid measurements. There
is no personal data anywhere in this project, so GDPR imposes no constraint on
where I put it. If I claimed residency as the reason, an interviewer who knows
the regulation would catch it, and rightly.

The reasons that actually hold are weaker and more specific. The data is French
and European grid data, RTE is the French transmission operator, and I am
targeting roles in France. Putting French grid data in Paris is the answer that
needs no explanation. eu-west-3 is also nearer the source APIs than a US region,
though at the volumes here that is worth milliseconds, not euros.

Against that, eu-west-3 is one of the smaller AWS regions. It is more expensive
per GB of S3 than eu-west-1, and I have not measured by how much. It also gets
services later than the large regions, and I have not confirmed that EMR
Serverless is available there at all, which matters for the Day 29 scale run.

## Decision

Everything that stores or scans data goes in eu-west-3: the three S3 buckets,
the Glue catalog, Athena, Lambda, and the Iceberg warehouse. State lives there
too.

The one exception I am willing to make in advance is the one-off EMR Serverless
benchmark. If EMR Serverless is not in Paris, that single throwaway run happens
in eu-west-1 and the README says so plainly. Moving one benchmark is cheaper
than moving a lakehouse.

AWS Budgets and IAM are global and sit outside this decision.

## Consequences

Storage and query bills are slightly higher than they would be in Ireland. At
the volumes this project produces the difference is cents, and I would rather
have the story than the cents. I should still check the S3 and Athena pricing
pages for eu-west-3 before I put any cost figure in the README, because I am
not going to publish a number I did not read off the source.

If the EMR run does end up in eu-west-1, it reads S3 across regions and AWS
bills that transfer per GB. That could easily be the largest single line item
in the whole project, so I need to price it before running it rather than
after. Same day, same writeup.

The service-availability risk is real and I am accepting it: some service I
want in Week 4 may not exist in Paris. The mitigation is that nothing in the
architecture is region-pinned except S3 and Glue, and both of those are where
the data actually is.

## What would make me reverse this

A measured cost difference large enough to matter, or a service I need in the
critical path that Paris does not have and cannot be worked around. Neither
seems likely at this scale, but if it happens I will write ADR-0002a rather
than quietly editing this one.
