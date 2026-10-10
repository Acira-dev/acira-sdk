# Original Acira companions

These small transparent PNGs are still frames of the original Acira Browser
characters, rendered from its `src/renderer/src/mascots/{cast,states,renderer}.ts`
sources using the shared renderer. They are original Acira artwork, not substitute
characters. Pill uses the neutral waiting pose; Blip uses idle. Head framing and
the halftone print pass match the browser's existing component.

The dashboard ships the resulting images locally. It does not require Three.js,
WebGL, an image service, audio, or additional network requests to show them.
Companions can be animated gently, kept still (the default), or hidden in Settings.
Reduced motion suppresses all decorative animation. A stopped action remains still.

Annotations are displayed separately from the original masked screenshot. Both
the enlarged target view and full-page view use the captured viewport coordinates,
including device-pixel scaling. Missing coordinates never produce an invented box.
