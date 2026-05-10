const fs = require('fs');
const content = fs.readFileSync('static/app_pro.js', 'utf8');

let line = 1;
let col = 1;
let stack = [];
let inString = null; // '"', "'", "`"
let inComment = false; // 'line', 'block'

for (let i = 0; i < content.length; i++) {
    const char = content[i];
    const next = content[i+1];
    
    if (char === '\n') {
        line++;
        col = 1;
        if (inComment === 'line') inComment = false;
        continue;
    } else {
        col++;
    }

    // Handle comments
    if (!inString) {
        if (!inComment) {
            if (char === '/' && next === '/') {
                inComment = 'line';
                i++;
                continue;
            }
            if (char === '/' && next === '*') {
                inComment = 'block';
                i++;
                continue;
            }
        } else if (inComment === 'block') {
            if (char === '*' && next === '/') {
                inComment = false;
                i++;
                continue;
            }
            continue;
        } else if (inComment === 'line') {
            continue;
        }
    }

    if (inComment) continue;

    // Handle strings (ignoring escaped chars)
    if (inString) {
        if (char === '\\') {
            i++; // skip next char
            continue;
        }
        if (char === inString) {
            inString = null;
        }
        continue;
    }

    if (char === '"' || char === "'" || char === "`") {
        inString = char;
        continue;
    }

    // Trace braces
    if (char === '{') {
        stack.push({ line, col, type: '{' });
    } else if (char === '}') {
        if (stack.length === 0) {
            console.log(`❌ Extra closing brace '}' at line ${line}, col ${col}`);
        } else {
            stack.pop();
        }
    }
}

if (stack.length > 0) {
    console.log(`❌ Found ${stack.length} unclosed brace(s):`);
    stack.forEach(b => {
        console.log(`  - Unclosed '{' at line ${b.line}, col ${b.col}`);
    });
} else {
    console.log("✅ All braces are perfectly balanced!");
}
