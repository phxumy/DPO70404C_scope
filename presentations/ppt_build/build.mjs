import fs from "node:fs/promises";
import { Presentation, PresentationFile } from "@oai/artifact-tool";

const ROOT =
  "D:/my_files/IOP_after_20250418/academic/CR_gate_new/20260711true_experiment/connect_DPO70404C/data/scope_runs";

const IMG = {
  timingCoarse:
    `${ROOT}/precision-longitudinal-20260816_184351-de4cf4d3/precision_batches/` +
    `precision-longitudinal-20260816_184351-de4cf4d3-timing-precision-coarse-L10-b01/attempt_002/precision_analysis/precision_summary.png`,
  timingFine:
    `${ROOT}/precision-longitudinal-20260816_184351-de4cf4d3/precision_batches/` +
    `precision-longitudinal-20260816_184351-de4cf4d3-timing-precision-fine-L10-b01/attempt_001/precision_analysis/precision_summary.png`,
  ampCoarse:
    `${ROOT}/precision-longitudinal-20260816_184351-de4cf4d3/precision_batches/` +
    `precision-longitudinal-20260816_184351-de4cf4d3-amplitude-precision-coarse-L10-b01/attempt_001/precision_analysis/precision_summary.png`,
  dense:
    `${ROOT}/timing-response-20260816_213040-f8a63687/precision_batches/` +
    `timing-response-20260816_213040-f8a63687-timing-response-L10-b01/attempt_001/precision_analysis/timing_response_curve.png`,
  shotsScaling: `${ROOT}/timing-response-shots-20260817_120443-6d3f0901/shots_scaling/shots_scaling.png`,
  overlay: `${ROOT}/timing-response-shots-20260817_120443-6d3f0901/shots_scaling/shots_response_overlay.png`,
  drift: `${ROOT}/timing-response-shots-20260817_120443-6d3f0901/shots_scaling/flat_slope_drift.png`,
  xzero:
    `${ROOT}/timing-response-shots-20260817_120443-6d3f0901/precision_batches/` +
    `timing-response-shots-20260817_120443-6d3f0901-timing-response-s1000-b-L10-b01/attempt_003/precision_analysis/xzero_phase_histogram.png`,
};

async function readImage(path) {
  const bytes = await fs.readFile(path);
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
}

function addTitle(slide, text) {
  const shape = slide.shapes.add({
    geometry: "textbox",
    name: "title",
    position: { left: 60, top: 32, width: 1160, height: 58 },
    fill: "none",
    line: { style: "solid", fill: "none", width: 0 },
  });
  shape.text = text;
  shape.text.style = { fontSize: 32, bold: true, color: "#0f172a", wrap: "square" };
  return shape;
}

function addBody(slide, lines, position) {
  const shape = slide.shapes.add({
    geometry: "textbox",
    name: "body",
    position,
    fill: "none",
    line: { style: "solid", fill: "none", width: 0 },
  });
  shape.text.set(
    lines.map((line) => ({
      bulletCharacter: "•",
      marginLeft: 18,
      indent: -10,
      spaceAfter: 8,
      runs: [line],
    })),
  );
  shape.text.style = { fontSize: 16, color: "#1e293b", lineSpacing: 1.12, wrap: "square" };
  return shape;
}

async function addImage(slide, key, position, alt) {
  const blob = await readImage(IMG[key]);
  slide.images.add({
    blob,
    contentType: "image/png",
    alt,
    fit: "contain",
    position,
  });
}

function twoColumn(slide, title, lines, imageKey, imageAlt, imagePosition) {
  addTitle(slide, title);
  addBody(slide, lines, { left: 60, top: 110, width: 460, height: 560 });
  return imageKey
    ? addImage(slide, imageKey, imagePosition ?? { left: 560, top: 110, width: 660, height: 560 }, imageAlt)
    : Promise.resolve();
}

async function main() {
  const presentation = Presentation.create({ slideSize: { width: 1280, height: 720 } });

  const s1 = presentation.slides.add();
  s1.background.fill = "white";
  const t1 = s1.shapes.add({
    geometry: "textbox",
    name: "title",
    position: { left: 72, top: 250, width: 1136, height: 90 },
    fill: "none",
    line: { style: "solid", fill: "none", width: 0 },
  });
  t1.text = "AWG 纵场输出精度验证";
  t1.text.style = { fontSize: 44, bold: true, color: "#0f172a", alignment: "center" };
  const st1 = s1.shapes.add({
    geometry: "textbox",
    name: "subtitle",
    position: { left: 72, top: 360, width: 1136, height: 50 },
    fill: "none",
    line: { style: "solid", fill: "none", width: 0 },
  });
  st1.text = "时间精度、幅度精度与噪声来源的初步结论（2026-08）";
  st1.text.style = { fontSize: 20, color: "#475569", alignment: "center" };

  const s2 = presentation.slides.add();
  s2.background.fill = "white";
  twoColumn(s2, "测量对象与实验方法", [
    "测量对象：超导比特 CZ 门的 q9 纵向场（Z 线）模拟输出。",
    "AWG 采样率 2 GHz（0.5 ns/点），示波器 25 GHz（40 ps/点）。",
    "AUX 是固定触发参考，所有波形都以 AUX 上升沿作为共同时间零点。",
    "关心的是参数到真实输出的映射是否稳定、可重复、可精确控制，而不是波形是否理想。",
    "每个实验点都是独立完整的 M=20 序列，多个 shot 用于平均和统计。",
  ]);

  const s3 = presentation.slides.add();
  s3.background.fill = "white";
  twoColumn(s3, "怎么算出真实时移和幅度变化", [
    "取 0 位移点的平均波形 f(t) 作为固定参考模板。",
    "对每个点的平均波形 y(t) 做最小二乘拟合：",
    "y − f ≈ −Δt·f′(t) + g·(f − f̄) + c",
    "Δt：真实移动的时间（ps）；g：相对参考的幅度增益；c：直流偏置。",
    "Δt 和 g 在同一次线性拟合里同时解出，避免时间和幅度互相串扰。",
    "不移动波形、不做互相关。误差棒用 block-bootstrap：shot 分块重抽样、重拟合 2000 次，取 2.5%–97.5% 作为 95% 置信区间。",
  ]);

  const s3a = presentation.slides.add();
  s3a.background.fill = "white";
  twoColumn(s3a, "最小二乘拟合怎么解", [
    "对每个点：先算平均波形 y(t)，再算差值 d(t)=y(t)−f(t)。",
    "三个基：−f′(t) 对应时间移动 Δt；f(t)−f̄ 对应幅度增益 g；常数 1 对应直流偏置 c。",
    "写成矩阵 D≈X·β，其中 β=[Δt, g, c]。",
    "最小二乘解：β=(XᵀX)⁻¹XᵀD，代码里用奇异值分解求稳。",
    "一次求解同时得到 Δt、g、c，避免互相串扰。",
  ]);

  const s3b = presentation.slides.add();
  s3b.background.fill = "white";
  twoColumn(s3b, "g 和 c 分别是什么", [
    "c（直流偏置）：整条波形整体上下平移多少伏，主要来自基线漂移。",
    "g（相对幅度增益）：相对参考波形，脉冲形状竖直放大/缩小的比例，无量纲。",
    "g=0 表示和参考一样；g=+0.024 表示放大约 2.4%。",
    "g 对应实验里的 q0ampback：每变 1 个单位，幅度相对变化约 2.4 个 g。",
    "例：参考峰峰约 86 mV，g=+0.0024 对应峰峰变化约 0.21 mV。",
  ]);

  const s4 = presentation.slides.add();
  s4.background.fill = "white";
  addTitle(s4, "触发相位直方图说明");
  addBody(s4, [
    "每帧的真实时间 = 触发瞬间 + XZERO + 40 ps × 采样点序号。",
    "XZERO 是真实触发瞬间到最近采样点之间小于 40 ps 的偏移。",
    "这张图把每一帧的 XZERO 按 40 ps 取余后统计分布。",
    "分布越均匀，说明触发相位覆盖了所有亚采样位置，为亚采样时间分辨提供条件。",
    "本图来自 s1000_b 的 attempt_003。",
  ], { left: 60, top: 110, width: 440, height: 520 });
  await addImage(s4, "xzero", { left: 540, top: 120, width: 680, height: 480 }, "触发相位直方图");

  const s5 = presentation.slides.add();
  s5.background.fill = "white";
  await twoColumn(s5, "时间粗扫：50 ps 可分辨", [
    "扫描 ±50 ps、±10 ps（相对 0.25 ns 中心），每点 20 发。",
    "左下是实测位移对命令位移：50 ps 明显偏出 0，10 ps 落在噪声里。",
    "斜率约 1.15，方向正确；单发时间抖动约 50 ps。",
    "说明大台阶可分辨，小台阶被单发噪声淹没。",
  ], "timingCoarse", "时间粗扫 precision_summary");

  const s6 = presentation.slides.add();
  s6.background.fill = "white";
  await twoColumn(s6, "时间细扫：1 ps、5 ps 不可分辨", [
    "扫描 ±5 ps、±1 ps，每点 50 发。",
    "点的均值没有跟着命令走，有的正负号都反了，拟合斜率甚至为负。",
    "说明信号远小于噪声底，不能声称 1 ps、5 ps 可分辨。",
  ], "timingFine", "时间细扫 precision_summary");

  const s7 = presentation.slides.add();
  s7.background.fill = "white";
  await twoColumn(s7, "幅度扫描：0.001 单位可分辨", [
    "扫描变量 q0ampback（无量纲），基准 0.35。",
    "左下是实测幅度增益对命令幅度变化，斜率约 2.4。",
    "0.01、0.005、0.001 可分辨；0.0005、0.0001 不可分辨。",
    "说明幅度响应线性，分辨率下限约 0.001 单位。",
  ], "ampCoarse", "幅度扫描 precision_summary");

  const s8 = presentation.slides.add();
  s8.background.fill = "white";
  await twoColumn(s8, "密集时间响应：整体斜率约 1.001", [
    "0、±2、±4、…、±120 ps，共 121 点，每点 140 发。",
    "斜率 1.001，截距 1.4 ps，残差 RMS 7.8 ps。",
    "平均意义下，命令移多少就真实移多少。",
    "但单点仍有约 ±10 ps 的误差棒。",
  ], "dense", "密集时间响应曲线", { left: 560, top: 100, width: 660, height: 540 });

  const s9 = presentation.slides.add();
  s9.background.fill = "white";
  await twoColumn(s9, "多发平均的噪声地板约 6 ps", [
    "同一组点分别用 20、100、500、1000 发。",
    "95% 置信区间半宽：34.5 → 12.6 → 7.2 → 6.0 ps。",
    "500 发到 1000 发只从 7.2 降到 6.0，仍有约 6 ps 的平台。",
    "拟合 CI ∝ N^(−0.44)，接近纯随机噪声的 0.5。",
  ], "shotsScaling", "噪声随发数的变化");

  const s10 = presentation.slides.add();
  s10.background.fill = "white";
  await twoColumn(s10, "不同发数下的响应曲线", [
    "直观看到误差棒随发数变小。",
    "500 发和 1000 发几乎重合，进入平台。",
    "响应本身始终贴近 1:1。",
  ], "overlay", "不同发数响应叠加");

  const s11 = presentation.slides.add();
  s11.background.fill = "white";
  await twoColumn(s11, "已定位：平顶下垂是示波器线缆接触问题", [
    "修紧线缆后重采，7 个 batch 的平顶斜率全部回到约 0.04 mV/ns。",
    "之前 b、c 约 0.14 mV/ns 的下垂已经消失。",
    "0 点的数字 SRAM 在所有批次完全一致，证明不是数字端或时移问题。",
  ], "drift", "平顶斜率随采集时间的变化");

  const s12 = presentation.slides.add();
  s12.background.fill = "white";
  twoColumn(s12, "目前能确定的结论", [
    "时间响应的平均映射准确：斜率 ≈ 1.001，命令多少就移多少。",
    "单点时间精度地板约 6 ps，拟合指数 α≈0.44。",
    "1–5 ps 的个位数时移目前不能作为可靠分辨率。",
    "幅度响应线性良好，0.001 单位可分辨。",
    "平顶下垂已定位为示波器线缆接触问题，修紧后消除。",
  ]);

  const out =
    "D:/my_files/IOP_after_20250418/academic/CR_gate_new/20260711true_experiment/connect_DPO70404C/presentations/AWG纵场输出精度验证.pptx";
  const pptx = await PresentationFile.exportPptx(presentation);
  await pptx.save(out);
  console.log("saved pptx:", out);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
