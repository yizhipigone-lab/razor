const fs = require('fs');
const file = 'e:/1target/p9_project/quant-platform/static/js/main.js';
const lines = fs.readFileSync(file, 'utf8').split('\n');

// Find and fix index normalization in the chart override function
// Search for "var aligned = eqDates.map" in the override section (around line 4140)
for (let i = 0; i < lines.length; i++) {
  if (lines[i].includes("var aligned = eqDates.map") && lines[i].includes("idxMap") && i > 4100) {
    console.log(`Found at line ${i+1}: ${lines[i].trim()}`);

    // Find the idxRet line (within next 3 lines)
    let idxRetLine = -1;
    for (let j = i; j < Math.min(i + 5, lines.length); j++) {
      if (lines[j].includes('var idxRet')) {
        idxRetLine = j;
        break;
      }
    }
    if (idxRetLine < 0) {
      console.log('Could not find idxRet line');
      break;
    }

    console.log(`idxRet at line ${idxRetLine+1}: ${lines[idxRetLine].trim()}`);

    // Replace lines i through idxRetLine
    const newLines = [
      '          var alignedRaw = eqDates.map(function(d) { return idxMap[d] || null; });',
      '          // 重新归一化：回测期间第一个有效值 = 1',
      '          var firstValid = null;',
      '          for (var k = 0; k < alignedRaw.length; k++) {',
      '            if (alignedRaw[k] !== null) { firstValid = alignedRaw[k]; break; }',
      '          }',
      '          var aligned = alignedRaw.map(function(v) { return v !== null && firstValid ? v / firstValid : null; });',
      '          var idxFinal = aligned.filter(function(v) { return v !== null; });',
      '          var idxRet = idxFinal.length > 0 ? ((idxFinal[idxFinal.length-1] - 1) * 100).toFixed(1) : "N/A";',
    ];

    lines.splice(i, idxRetLine - i + 1, ...newLines);
    console.log('Replaced successfully');
    break;
  }
}

// Also fix the tooltip formatter - use relative returns
for (let i = 0; i < lines.length; i++) {
  if (lines[i].includes("var ret = ((p.value - 1) * 100).toFixed(2);") && i > 4150) {
    console.log(`Tooltip ret at line ${i+1}`);
    // The tooltip now uses re-normalized values, so (value-1)*100 should be correct
    // since we re-normalized to first_valid=1
    console.log('Tooltip calculation is now correct with re-normalized values');
    break;
  }
}

fs.writeFileSync(file, lines.join('\n'), 'utf8');
console.log('Done');
