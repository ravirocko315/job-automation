#let payload = json(bytes(sys.inputs.payload))
#let basics = payload.basics
#let compact = payload.at("compact", default: false)

#set page(
  paper: "us-letter",
  margin: if compact { (x: 0.45in, y: 0.4in) } else { (x: 0.55in, y: 0.5in) },
)
#set text(
  font: ("Calibri", "Arial", "Liberation Sans"),
  size: if compact { 9.5pt } else { 10pt },
  fill: rgb("#111111"),
)
#set par(leading: if compact { 0.52em } else { 0.6em }, justify: true)

// --- HEADER ---
#align(center)[
  #text(size: 18pt, weight: "bold")[#basics.first_name #basics.last_name] \
  #v(2pt)
  #text(size: 9pt, fill: rgb("#333333"))[
    #basics.location.city, #basics.location.state | #basics.phone | #basics.email \
    #basics.links.linkedin | #basics.links.github | #basics.links.portfolio
  ]
]

#v(4pt)
#line(length: 100%, stroke: 0.6pt + rgb("#333333"))

// --- SKILLS ---
#text(size: 11pt, weight: "bold")[TECHNICAL SKILLS]
#v(2pt)
#text(size: 9.5pt)[
  *Core & Matched Skills:* #payload.skills_line
]

#v(6pt)
#line(length: 100%, stroke: 0.4pt + rgb("#cccccc"))

// --- EXPERIENCE ---
#text(size: 11pt, weight: "bold")[PROFESSIONAL EXPERIENCE]
#v(3pt)

#for exp in payload.experiences [
  #grid(
    columns: (1fr, auto),
    [*#exp.company* — _#exp.title _],
    [*#exp.start_date – #exp.end_date* | #exp.location]
  )
  #v(2pt)
  #for b in exp.bullets [
    - #b
  ]
  #v(5pt)
]

#line(length: 100%, stroke: 0.4pt + rgb("#cccccc"))

// --- EDUCATION ---
#text(size: 11pt, weight: "bold")[EDUCATION]
#v(3pt)

#for edu in payload.education [
  #grid(
    columns: (1fr, auto),
    [*#edu.institution* — #edu.degree (GPA: #edu.gpa)],
    [*Graduated: #edu.graduation_date*]
  )
]