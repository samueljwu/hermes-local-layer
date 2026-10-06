# Composite Evidence

## Nested list fenced code

- Positive code control [^code-positive] ^[raw/papers/code-positive.md].
  ```text
  [^code-fake]: literal [^code-fake] ^[raw/papers/code-fake.md]
  ```
  Positive code tail [^code-tail] ^[raw/papers/code-tail.md].

## Nested list equation

- Positive equation control [^equation-positive] ^[raw/papers/equation-positive.md].
  $$
  [^equation-fake]: literal [^equation-fake] ^[raw/papers/equation-fake.md]
  $$
  Positive equation tail [^equation-tail] ^[raw/papers/equation-tail.md].

## Multiline footnote

[^multi]: Positive footnote control [^footnote-positive] ^[raw/papers/footnote-positive.md].
    ```text
    [^footnote-fake]: literal [^footnote-fake] ^[raw/papers/footnote-fake.md]
    ```
    Positive footnote tail [^footnote-tail] ^[raw/papers/footnote-tail.md].

## Deep nested list fenced code

- Outer positive [^deep-outer] ^[raw/papers/deep-outer.md].
  - Child
    - Grandchild
      ```text
      [^deep-fake]: literal [^deep-fake] ^[raw/papers/deep-fake.md]
      ```
  Tail positive [^deep-tail] ^[raw/papers/deep-tail.md].
- Following top-level item remains outside the deep fence.
