const fs = require('fs');
const file = 'e:/1target/p9_project/quant-platform/static/js/main.js';
const lines = fs.readFileSync(file, 'utf8').split('\n');

// Fix non-override chart function (around line 3880-3900)
for (let i = 0; i < lines.length; i++) {
  if (lines[i].includes("var aligned = eqDates.map(d => idxMap[d] || null);") && i < 4100) {
    console.log(`Non-override found at line ${i+1}`);

    // Find next idxIdx++ or series.push
    let seriesLine = -1;
    for (let j = i; j < Math.min(i + 10, lines.length); j++) {
      if (lines[j].includes('idxIdx++')) {
        seriesLine = j;
        break;
      }
      if (lines[j].includes('series.push({') && lines[j-1] && !lines[j-1].includes('//')) {
        seriesLine = j - 1;
        break;
      }
    }
    if (seriesLine < 0) seriesLine = i + 6; // default

    const newLines = [
      '        const alignedRaw = eqDates.map(d => idxMap[d] || null);',
      '        // 重新归一化：回测期间第一个有效值 = 1',
      '        let firstValidIdx = null;',
      '        for (let k = 0; k < alignedRaw.length; k++) {',
      '          if (alignedRaw[k] !== null) { firstValidIdx = alignedRaw[k]; break; }',
      '        }',
      '        const aligned = alignedRaw.map(v => v !== null && firstValidIdx ? v / firstValidIdx : null);',
    ];

    lines.splice(i, seriesLine - i, ...newLines);
    console.log(`Replaced lines ${i+1} to ${seriesLine}`);
    break;
  }
}

fs.writeFileSync(file, lines.join('\n'), 'utf8');
console.log('Done');
