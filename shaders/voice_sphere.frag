#version 460 core
#include <flutter/runtime_effect.glsl>

// Glowing voice sphere — the AI-provider speech orb.
// The body is a flowing teal / aqua / amber / warm-white gas tuned for the cream
// theme: several noise fields are advected in DIFFERENT directions and
// domain-warped so the colors churn non-uniformly, like gas. Voice state only
// changes motion speed + brightness (uIntensity), never the palette.
//
// Float uniform indices (declaration order; used by setFloat on the Dart side):
//   0 uTime | 1 uSize.x | 2 uSize.y | 3 uIntensity

out vec4 fragColor;

uniform float uTime;
uniform vec2  uSize;
uniform float uIntensity;

// --- value noise + fbm ------------------------------------------------------
float hash(vec2 p) {
  p = fract(p * vec2(123.34, 456.21));
  p += dot(p, p + 45.32);
  return fract(p.x * p.y);
}

float noise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  float a = hash(i + vec2(0.0, 0.0));
  float b = hash(i + vec2(1.0, 0.0));
  float c = hash(i + vec2(0.0, 1.0));
  float d = hash(i + vec2(1.0, 1.0));
  return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

float fbm(vec2 p) {
  float v = 0.0;
  float amp = 0.5;
  for (int i = 0; i < 5; i++) {
    v += amp * noise(p);
    p = p * 2.0 + 7.13;
    amp *= 0.5;
  }
  return v;
}

void main() {
  // Centered, aspect-correct coords. Sphere body within radius R, margin = halo.
  vec2 uv = (FlutterFragCoord().xy / uSize) * 2.0 - 1.0;
  float R = 0.72;
  vec2 sp = uv / R;                 // sphere-space: surface at length(sp) == 1
  float r = length(sp);

  float sphereMask = 1.0 - smoothstep(0.97, 1.0, r);

  // Fake 3D normal; light from up-left -> lit-glass look.
  float z = sqrt(max(0.0, 1.0 - r * r));
  vec3 normal = vec3(sp, z);
  vec3 lightDir = normalize(vec3(-0.5, 0.6, 0.85));
  float diffuse = clamp(dot(normal, lightDir), 0.0, 1.0);
  float spec = pow(diffuse, 18.0);

  // Sample the gas slightly "into" the sphere so it wraps around the surface.
  vec2 g = sp * (1.0 + 0.35 * (1.0 - z));

  // Time speed scales with state: calm at idle, fast when active.
  float t = uTime * (0.30 + 0.55 * uIntensity);

  // Domain-warped flow, each field drifting a DIFFERENT direction => non-uniform.
  vec2 q = vec2(
    fbm(g * 1.6 + vec2( 0.0,  0.30) * t),
    fbm(g * 1.6 + vec2( 5.2,  1.30) + vec2(-0.22, 0.0) * t)
  );
  vec2 w = vec2(
    fbm(g * 1.9 + 2.0 * q + vec2(1.7, 9.2) + vec2( 0.18, -0.12) * t),
    fbm(g * 1.9 + 2.0 * q + vec2(8.3, 2.8) + vec2(-0.10,  0.20) * t)
  );
  float f = fbm(g * 2.3 + 2.4 * w);

  // Palette — the four gases, tuned to sit on cream (teal accent anchor,
  // softened aqua, warm amber/clay instead of harsh red, warm-white highlight).
  vec3 TEAL = vec3(0.12, 0.78, 0.69);
  vec3 AQUA = vec3(0.30, 0.62, 0.78);
  vec3 AMBER = vec3(0.90, 0.62, 0.34);
  vec3 WARM = vec3(0.99, 0.96, 0.90);

  // Layer the gases by the independent fields.
  vec3 col = TEAL;
  col = mix(col, AQUA,  smoothstep(0.30, 0.85, q.x));
  col = mix(col, AMBER, smoothstep(0.35, 0.95, w.y));
  col = mix(col, WARM,  smoothstep(0.55, 1.05, pow(f, 1.6) * 1.4));

  // Sphere shading as brightness on top of the gas color.
  float shade = mix(0.45, 1.0, diffuse);
  float fresnel = pow(clamp(r, 0.0, 1.0), 3.0);
  float brightness = shade * (0.80 + 0.45 * uIntensity) + fresnel * 0.30;

  col *= brightness;
  col += vec3(spec * 0.65);          // glossy white highlight

  // Outer halo glow beyond the sphere body, tinted by the current gas color.
  float halo = exp(-3.2 * max(0.0, r - 0.7)) * (0.22 + 0.40 * uIntensity);
  float alpha = clamp(sphereMask + halo * (1.0 - sphereMask), 0.0, 1.0);

  // Radial cutoff: force alpha to 0 on a circle inside the square canvas so the
  // glow never reaches the rect edges/corners (no square outline).
  alpha *= 1.0 - smoothstep(1.02, 1.30, r);

  // Premultiplied alpha (Flutter requirement).
  fragColor = vec4(col * alpha, alpha);
}
