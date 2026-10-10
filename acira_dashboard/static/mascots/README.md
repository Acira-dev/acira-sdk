# Original Acira companions

These small transparent PNGs are still frames of the original Acira Browser
characters, rendered from its `src/renderer/src/mascots/{cast,states,renderer}.ts`
sources using the shared renderer. They are original Acira artwork, not substitute
characters. Pill uses the neutral waiting pose; Blip uses idle. Head framing and
the halftone print pass match the browser's existing component.

`pill-perched.png` and `pill-perched-swing.png` use the original full-body Pill
geometry, face, materials and print renderer with a neutral seated pose. The latter
is a 24-frame horizontal sprite sheet (196 × 232 pixels per frame), moving the
original two hip joints in opposing phases over a 3.2-second loop. The projected
seat is 176.736 pixels below the frame top; the UI aligns it with the preview border
and reserves space for the feet outside the screenshot. Only pending reviews move.

The dashboard ships the resulting images locally. It does not require Three.js,
WebGL, an image service, audio, or additional network requests to show them.
Companions can be animated gently, kept still (the default), or hidden in Settings.
Reduced motion suppresses all decorative animation. A stopped action remains still.

Annotations are displayed separately from the original masked screenshot. Both
the enlarged target view and full-page view use the captured viewport coordinates,
including device-pixel scaling. Missing coordinates never produce an invented box.
The visual ring has six viewport pixels of clearance outside those exact bounds.
