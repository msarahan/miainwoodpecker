# Welcome to miainwoodpecker's documentation

:::{toctree}
:maxdepth: 2
:hidden:
:caption: Contents:

Home <self>
Migration plan <migration-plan>
Hardware validation checklist <hardware-validation-checklist>
Documentation <documentation/index>
:::

This documentation uses myst as the primary documentation syntax.

:::{button-link} <https://myst-parser.readthedocs.io/en/latest/syntax/syntax.html>
:color: primary
:class: sd-rounded-pill float-left

Learn more about myst markdown syntax.

:::

Myst is a version of markdown that has more formatting flexibility.
This is what a sphinx directive looks like using myst markdown formatting:

```markdown
:::{toctree}
:maxdepth: 2
:caption: Contents:
:::

```

If you see syntax like the syntax below, you are looking at rst.

```rst
.. toctree::
   :maxdepth: 2
   :caption: Contents:
```
