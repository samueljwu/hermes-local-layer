# Fence Recovery

## Invalid info

```bad`info
literal invalid opener
### After invalid info

Recovered invalid prose.

## Unclosed backtick

```text
literal [^code] ^[raw/papers/code.md]
### After unclosed backtick

Recovered backtick.

## Unclosed tilde

~~~ocr
OCR text
### After unclosed tilde

Recovered tilde.

## OCR tilde

~~~~~~~~~~~E
(1)
### OCR heading after suspect opener

Recovered OCR heading.

## Unclosed equation

$$
x = y
### After unclosed equation

Recovered equation.
