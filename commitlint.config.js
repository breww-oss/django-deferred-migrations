module.exports = {
    extends: ['@commitlint/config-angular'],
    rules: {
        'type-enum': [
            2,
            'always',
            [
                // Default: https://github.com/conventional-changelog/commitlint/blob/master/%40commitlint/config-angular-type-enum/index.js
                'build',
                'ci',
                'docs',
                'feat',
                'fix',
                'perf',
                'refactor',
                'revert',
                'style',
                'test',
                // Added: https://github.com/conventional-changelog/commitlint/#what-is-commitlint
                'chore',
            ],
        ],
    },
};
