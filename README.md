<p align="center">
  <img src="assets/logo.png" alt="InfiniDream" width="340">
</p>

<h3 align="center">Open-Ended Photorealistic 3D Worlds through Procedural Generation</h3>

<p align="center">
  <a href="https://yichuanh.github.io/">Yi-Chuan Huang</a><sup>1,2</sup> ·
  <a href="https://chien90190.github.io/">Hao-Jen Chien</a><sup>2</sup> ·
  <a href="https://brian90709.github.io/">Zheng-Hui Huang</a><sup>1,3</sup> ·
  <a href="https://yulunalexliu.github.io/">Yu-Lun Liu</a><sup>2</sup> ·
  <a href="https://kpzhang93.github.io/">Kaipeng Zhang</a><sup>1</sup> ·
  <a href="https://lightchaserx.github.io/">Zhixiang Wang</a><sup>1</sup>
</p>

<p align="center">
  <sup>1</sup>Alaya Lab &nbsp; <sup>2</sup>National Yang Ming Chiao Tung University &nbsp; <sup>3</sup>National Taiwan University
</p>

<p align="center">
  <a href="https://yichuanh.github.io/infinidream-page/"><img src="https://img.shields.io/badge/Project-Page-2dd4bf" alt="Project Page"></a>
  <a href="#citation"><img src="https://img.shields.io/badge/Paper-coming%20soon-lightgrey" alt="Paper"></a>
  <img src="https://img.shields.io/badge/Code-coming%20soon-orange" alt="Code coming soon">
</p>

---

InfiniDream turns a single text prompt into a photorealistic, open-ended 3D world. A VLM plans the scene and edits sophisticated procedural code rather than raw geometry, so every asset stays high-quality, every scene stays coherent, and a new seed grows a new world.

⭐ **Code is on the way. Star the repo to get notified when it lands.**

## From a sentence to a living world

<p align="center"><img src="assets/teaser.gif" width="720" alt="Scene build-up"></p>

Objects are planned, generated, and placed one by one into a living, animated scene.

## Worlds InfiniDream dreamed up

<p align="center"><img src="assets/worlds.gif" width="720" alt="Scene gallery: cliff, coral reef, sakura, snowy mountain, flamingo coast, desert"></p>

Every scene starts as one sentence. Twenty worlds and counting, all animated, all procedural.

## Quality beyond existing VLM methods

<p align="center"><img src="assets/comparison.gif" width="720" alt="VLM-driven methods vs InfiniDream"></p>

The same objects, written by VLM-driven baselines and by InfiniDream. Each tile wipes from the baseline to ours.

## New seed, new variant

<p align="center"><img src="assets/diversity.gif" width="720" alt="Seed diversity"></p>

One program per object. Swapping the seed produces endless high-quality variants, which makes large diverse scenes cheap to build.

## Precise edits, whole worlds

<p align="center"><img src="assets/editing.gif" width="720" alt="Before and after"></p>

A vanilla procedural scene, before and after InfiniDream rewrites it to match the prompt.

## Citation

```bibtex
@misc{infinidream2026,
  title  = {InfiniDream: Open-Ended Photorealistic 3D Worlds
            through Procedural Generation},
  author = {Huang, Yi-Chuan and Chien, Hao-Jen and Huang, Zheng-Hui
            and Liu, Yu-Lun and Zhang, Kaipeng and Wang, Zhixiang},
  year   = {2026}
}
```
