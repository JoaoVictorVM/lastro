# Cadastro de funcionário

Documentação funcional sintética da aplicação demo do Lastro. Ela descreve as regras declaradas
para o cadastro de funcionários de uma clínica. É material de avaliação: não contém dados reais.

## Campos obrigatórios

- O nome completo é obrigatório e deve ter entre 3 e 120 caracteres.
- O CPF é obrigatório e precisa ser um CPF válido.
- O perfil do funcionário é obrigatório.
- A data de admissão é obrigatória.
- O email é opcional no cadastro de funcionário.

```lastro-fact
{"flow": "employee.create", "category": "screen_component",
 "fact": {"type": "field_property", "subject": "employee", "field": "email",
          "attribute": "required", "value": false}}
```

## Campos opcionais

O telefone é opcional. Quando informado, deve seguir o formato (11) 91234-5678 para celular ou
(11) 3456-7890 para fixo. O email, quando informado, precisa ter um formato de email válido.

## Regras de CPF

O mesmo CPF não pode ser cadastrado duas vezes na mesma clínica. Clínicas diferentes podem ter
funcionários com o mesmo CPF, porque cada clínica mantém o próprio cadastro.

## Data de admissão

A data de admissão não pode estar no futuro. Datas passadas são aceitas, inclusive para
funcionários admitidos antes da implantação do sistema.

## Status

Todo funcionário novo começa com o status Ativo. Os status possíveis são Ativo e Inativo.

## Mensagens de validação

Quando um campo é recusado, a mensagem aparece ao lado do campo e o formulário mantém os valores
já digitados, para que a pessoa corrija só o que falhou.
