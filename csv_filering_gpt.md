FIRST DO THIS:
These are all jobs urls, go to each URL and rank it from 0 to 10 whether I should apply for it or not.
I did not ask you to separate jobs by any criteria I just want you to rank it from 0 to 10 whether or not I should apply based on description only nothing else, not location, no bullshit, just description. Save it as a new csv file


THEN DO THIS:
## Reusable filtering pipeline

Ordered from **lowest to highest computational cost**.

### 1. CSV normalization and preservation

**Cost: negligible**

* Use comma-separated CSV.
* Quote every field.
* Preserve the original URL and original row identifier.
* Keep the existing ranking order unless explicitly re-sorting.
* Use UTF-8 encoding.

This step does not remove jobs, but it should be applied whenever a new filtered CSV is saved.

---

### 2. Remove jobs with a score of 2 or less
### 3. Remove positions below mid-senior level
### 4. Remove explicitly non-North-American locations
Do not automatically remove a row solely because the location field is blank or malformed. Mark it for later verification.
### 5. Keep only Ottawa or remote jobs

Ottawa-compatible labels can include:

```text
Ottawa
Ottawa, Ontario
Ottawa, ON
National Capital Region
Kanata
Nepean
Gloucester
Orléans
Gatineau, when treating the Ottawa–Gatineau region as acceptable
```

Remote-compatible wording can include:

```text
remote
fully remote
remote anywhere
remote Canada
Canada remote
work from anywhere
distributed
home-based
telecommute
```

Remove jobs that are clearly:

* On-site outside Ottawa
* Hybrid outside Ottawa
* Tied to another city with no remote option
* Remote only within an incompatible country or jurisdiction, when that restriction is stated

When location or remote eligibility cannot be established, retain the row temporarily and add:

```text
ottawa_or_remote_status = Uncertain
location_uncertain = Yes
location_filter_basis = explanation
```

---

### 6. Remove obviously non-IT roles

Examples removed:

* Fundraising and donor development
* Communications and campaign coordination
* Public-health program consulting
* Social-work program management
* Supportive-housing program management
* Automotive sales
* Automotive parts sales
* Construction project management
* Environmental project management
* Environmental remediation engineering
* Energy project development
* Mining project engineering
* Mechanical design engineering
* Manufacturing engineering
* Materials engineering
* Optical engineering with no software component
* Aircraft support engineering with no software component
* Marine project management
* Operating engineer
* Process-development engineering for physical manufacturing
* Construction-materials engineering
* Architect roles unrelated to information architecture or software architecture
* Company news articles rather than job postings
* Product or service pages rather than job postings

Keep roles clearly associated with:

* Software development
* AI and machine learning
* Data engineering
* Analytics engineering
* Databases
* Cloud security
* Cybersecurity
* Application security
* Software architecture
* Technical product management
* Software implementation
* Business intelligence engineering
* API platforms
* Technical consulting
* Systems software
* Developer tooling

When an IT relationship is plausible but not established, retain the row and add:

```text
it_relevance_status = Uncertain
it_filter_basis = explanation
```

Examples that may require an uncertainty label:

* Generic Project Manager
* Generic Program Manager
* Generic Developer
* Generic Engineer
* Automation Engineer
* Systems Engineer
* Test Engineer
* Implementation Lead
* Design and Support Engineer

---

### 7. Remove unwanted role families and technology stacks identifiable from titles

Remove jobs primarily focused on any of the following.

.NET and C#

Indicators:

```text
.NET
dotnet
ASP.NET
C#
C sharp
Blazor
Entity Framework
```

Frontend development

Indicators:

```text
frontend
front-end
front end
web developer
UI developer
Next.js
NextJS
React developer
Angular developer
Vue developer
JavaScript frontend
TypeScript frontend
HTML/CSS-focused
```

Do not remove a true backend or data role merely because it interacts with a frontend.

Rust

Indicators:

```text
Rust
Rust developer
Rust engineer
```

iOS

Indicators:

```text
iOS
Swift
Objective-C
Apple mobile
iPhone application
```

Systems administration

Indicators:

```text
sysadmin
system administrator
systems administrator
IT administrator
infrastructure support
server administrator
site infrastructure support
desktop administrator
```

DevOps

Indicators:

```text
DevOps
DevSecOps
site reliability engineer
SRE
platform operations
CI/CD engineer
release engineer
infrastructure engineer
cloud operations
developer infrastructure
build and release
```

A developer-infrastructure leadership position was also removed because its central function aligned with DevOps and engineering infrastructure.

Golang

Indicators:

```text
Golang
Go engineer
Go developer
Go runtime
```

Be careful with the standalone word `go`; it should not be used as a raw substring match.


---

### 8. Remove business analyst and QA jobs


#### Business analyst exclusions

Remove titles or descriptions centered on:

```text
business analyst
business systems analyst
functional analyst
requirements analyst
process analyst
business process analyst
business analysis
requirements gathering
stakeholder requirements
functional specifications
business requirements documentation
BRD
```

Do not automatically remove:

* Analytics Engineer
* Data Analyst
* Business Intelligence Engineer
* Product Manager

Those are separate categories unless the description is fundamentally a business-analysis role.

#### QA exclusions

Remove:

```text
quality assurance
QA engineer
QA specialist
software tester
test engineer
test automation engineer
SDET
software development engineer in test
validation engineer
verification engineer
quality engineer
manual tester
automation tester
```

Examples removed:

* Software Development Engineer in Test
* Test Automation Engineer
* Quality Assurance Specialist
* Test Engineer
* Embedded test-automation developer

---

### 9. Remove C++, embedded, firmware, and hardware jobs

Remove jobs where the principal work involves:

#### C++

Indicators:

```text
C++
modern C++
C/C++
STL
Boost
Qt C++
```

#### Embedded software and firmware

Indicators:

```text
embedded
firmware
microcontroller
MCU
bare metal
real-time operating system
RTOS
device driver
kernel driver
board support package
BSP
hardware bring-up
ARM
RISC-V
FPGA integration
```

#### Hardware and chip design

Indicators:

```text
hardware engineer
electrical engineer
analog design
digital design
chip design
ASIC
SoC
silicon
chiplet
memory architect
CPU architect
PCB
circuit
electronics
semiconductor
physical design
verification
```

#### Hardware-adjacent systems

Remove when the job is fundamentally about:

* Robotics hardware
* Spacecraft systems
* Guidance, navigation, and control
* Camera drivers
* Imaging hardware
* GPU drivers
* Audio hardware stacks
* Network appliances
* Routers, switches, and physical firewalls
* Industrial machinery
* Manufacturing automation
* Nuclear or mechanical systems
* Sensor and control firmware

Examples removed:

* Senior Embedded Software Developer — Quantum Control Firmware
* RISC-V AI and HPC Engineering Lead
* Senior Network Management Software Engineer
* Chiplet Design Architect
* Memory Architect
* Embedded Linux Consultant
* GPU Consultant Engineer
* Linux Kernel Consultant
* Camera Driver Software Developer
* Robotics Engineer
* Analog Design Engineer
* Linux Audio Stack Engineer
* Network and Security Consultant focused on physical network infrastructure

This combined QA, C#, C++, and hardware stage removed **20 jobs**, leaving **48**.

---

## 10. Analyze the available job description

**Cost: high**

When a title is insufficient, inspect the description to determine the job’s actual primary function.

Use the description to answer:

1. What work occupies most of the role?
2. Which technologies are required rather than merely mentioned?
3. Is an excluded technology central or incidental?
4. Is the role actually software, data, AI, product, QA, hardware, DevOps, or business analysis?
5. Does “systems” mean software systems or physical engineering systems?
6. Does “automation” mean software workflow automation or industrial controls?
7. Does “developer” refer to software, real estate, cosmetics, fundraising, or another unrelated meaning?
8. Is the page an actual vacancy rather than a developer portal, documentation page, news article, career story, or generic company page?

Remove a job only when the excluded function is a meaningful part of the actual role, not because one unwanted technology appears in a minor “nice to have” list.

---

## 11. Access the live URL for unresolved cases

**Cost: highest**

Open the page only when the CSV fields and captured description do not establish:

* Actual job title
* Whether it is a real active job posting
* Location
* Remote eligibility
* Seniority
* IT relevance
* Primary technology stack
* Whether the role is QA, DevOps, frontend, hardware, embedded, business analysis, or another excluded category

Live-page checks should also detect:

* Redirects to a generic careers page
* Expired jobs
* Documentation portals
* Developer portals
* Videos
* News articles
* Mailto links
* Product pages
* Company profile pages
* Broken or inaccessible pages
* Pages whose visible title was scraped incorrectly

For unresolved cases, retain the row rather than guessing and add fields such as:

```text
review_status = Uncertain
uncertainty_reason = Location unavailable
uncertainty_reason = Full description inaccessible
uncertainty_reason = Technology stack unclear
uncertainty_reason = Role domain unclear
uncertainty_reason = Page may not be a job posting
```

---

## Full filter specification in compact form

```text
1. Normalize CSV: UTF-8, commas, quote all fields.
2. Remove score <= 2.
3. Remove junior, internship, co-op, entry-level, associate, trainee, and level-I jobs.
4. Remove jobs explicitly outside North America.
5. Keep only Ottawa or remote; retain unresolved locations as Uncertain.
6. Remove clearly non-IT roles and non-job pages.
7. Remove .NET and C# roles.
8. Remove frontend roles.
9. Remove Rust roles.
10. Remove iOS roles.
11. Remove sysadmin and infrastructure-support roles.
12. Remove DevOps, SRE, CI/CD, cloud-operations, release, and developer-infrastructure roles.
13. Remove Golang roles.
14. Remove business analyst roles.
15. Remove QA, testing, SDET, validation, and test-automation roles.
16. Remove C++ roles.
17. Remove embedded, firmware, kernel, driver, and low-level systems roles.
18. Remove hardware, chip, electronics, robotics-hardware, industrial-controls, and physical-network-infrastructure roles.
19. Analyze descriptions for ambiguous cases.
20. Open live URLs only for cases still unresolved.
21. Preserve unresolved rows with an Uncertain status instead of guessing.
```

