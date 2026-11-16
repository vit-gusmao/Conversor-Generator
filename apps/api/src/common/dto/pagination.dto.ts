import { Type } from 'class-transformer';
import { IsInt, IsOptional, IsString, Max, MaxLength, Min } from 'class-validator';

export const DEFAULT_PAGE_SIZE = 20;
export const MAX_PAGE_SIZE = 100;

/** `?page=1&page_size=20` — 1-based page, hard cap of 100 items. */
export class PaginationQueryDto {
  @IsOptional()
  @Type(() => Number)
  @IsInt({ message: 'page deve ser um número inteiro' })
  @Min(1, { message: 'page deve ser maior ou igual a 1' })
  page: number = 1;

  @IsOptional()
  @Type(() => Number)
  @IsInt({ message: 'page_size deve ser um número inteiro' })
  @Min(1, { message: 'page_size deve ser maior ou igual a 1' })
  @Max(MAX_PAGE_SIZE, { message: `page_size deve ser no máximo ${MAX_PAGE_SIZE}` })
  page_size: number = DEFAULT_PAGE_SIZE;

  get skip(): number {
    return (this.page - 1) * this.page_size;
  }
}

export class ListConversionsQueryDto extends PaginationQueryDto {
  @IsOptional()
  @IsString({ message: 'status deve ser um texto' })
  @MaxLength(64)
  status?: string;

  @IsOptional()
  @IsString({ message: 'q deve ser um texto' })
  @MaxLength(200)
  q?: string;
}
